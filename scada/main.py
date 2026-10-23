"""SCADA 上位机: Modbus 采集 + WebSocket 实时推送 + 历史库 + 操作命令下发。

数据流
------
    plant(Modbus Server, :5020)
        ^  |  PV / MV
        |  v
    plc(Modbus Client)  --写泵指令-->  plant

    scada(Modbus Client) --读 PV/状态--> plant   (本文件)
                        <--WebSocket-->  浏览器

本进程不做任何控制运算, 只负责采集、存储、呈现和把操作员意图写回现场。
控制算法全部在 controller/plc.py 中 —— 这是 SCADA 与 PLC 的职责边界。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pymodbus.client import AsyncModbusTcpClient  # noqa: E402

from common import registers as R  # noqa: E402

log = logging.getLogger("scada")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DB_PATH = os.environ.get("SCADA_DB", os.path.join(HERE, "scada.db"))

POLL_S = 0.1            # Modbus 采集周期
PUSH_S = 0.1            # WebSocket 推送周期
HISTORY_S = 1.0         # 历史入库周期
PLC_TIMEOUT_S = 3.0     # 心跳停止多久判定 PLC 离线

DEFAULTS = {
    "pump1_permille": 500,
    "pump2_permille": 450,
    "plc_mode": 0,
}


# ------------------------------------------------------------------ 数据库
class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts REAL PRIMARY KEY, t REAL,
                h1 REAL, h2 REAL, h3 REAL,
                q1 REAL, q2 REAL, q3 REAL,
                u1 REAL, u2 REAL,
                sp1 REAL, sp2 REAL,
                mode INTEGER, alarm INTEGER
            );
            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, code TEXT, level INTEGER, message TEXT, acked INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
            """
        )
        self.conn.commit()

    def add_sample(self, s: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["ts"], s["t"], s["levels"]["h1"], s["levels"]["h2"], s["levels"]["h3"],
                s["flows"]["q1"], s["flows"]["q2"], s["flows"]["q3"],
                s["pumps"]["u1"], s["pumps"]["u2"],
                s["sp"]["sp1"], s["sp"]["sp2"], s["mode"], s["alarm_word"],
            ),
        )
        self.conn.commit()

    def recent(self, seconds: float, limit: int = 4000) -> list[dict[str, Any]]:
        cutoff = time.time() - seconds
        cur = self.conn.execute(
            "SELECT ts,t,h1,h2,h3,q1,q2,q3,u1,u2,sp1,sp2,mode,alarm "
            "FROM samples WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
            (cutoff, limit),
        )
        cols = ["ts", "t", "h1", "h2", "h3", "q1", "q2", "q3", "u1", "u2", "sp1", "sp2", "mode", "alarm"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def add_alarm(self, code: str, level: int, message: str) -> None:
        self.conn.execute(
            "INSERT INTO alarms (ts, code, level, message) VALUES (?,?,?,?)",
            (time.time(), code, level, message),
        )
        self.conn.commit()

    def recent_alarms(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT ts, code, level, message FROM alarms ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {"ts": r[0], "code": r[1], "level": r[2], "message": r[3], "acked": False}
            for r in cur.fetchall()
        ]

    def trim(self, keep_seconds: float = 86400) -> None:
        self.conn.execute("DELETE FROM samples WHERE ts < ?", (time.time() - keep_seconds,))
        self.conn.commit()


# ------------------------------------------------------------------ 服务
class ScadaService:
    def __init__(self) -> None:
        self.store = Store(DB_PATH)
        # 注意: AsyncModbusTcpClient 构造时会抓取当前事件循环,
        # 因此只能在事件循环内部创建(见 run()), 不能在模块导入期实例化。
        self.client: AsyncModbusTcpClient | None = None
        self.connected = False
        self.latest: dict[str, Any] = {}
        self.clients: set[WebSocket] = set()
        self.last_hb: int | None = None
        self.last_hb_change = 0.0
        self.active_alarms: dict[str, int] = {}
        self.sensor_fault = False
        self._tasks: list[asyncio.Task] = []

    # -------------------------------------------------------------- 采集
    def _decode(self, ir: list[int], hr: list[int]) -> dict[str, Any]:
        levels = {
            "h1": ir[R.IR["h1_mm"]],
            "h2": ir[R.IR["h2_mm"]],
            "h3": ir[R.IR["h3_mm"]],
        }
        flows = {
            "q1": round(ir[R.IR["q1_lpm100"]] / 100.0, 2),
            "q2": round(ir[R.IR["q2_lpm100"]] / 100.0, 2),
            "q13": round(ir[R.IR["q13_lpm100"]] / 100.0, 2),
            "q23": round(ir[R.IR["q23_lpm100"]] / 100.0, 2),
            "q3": round(ir[R.IR["q3_lpm100"]] / 100.0, 2),
        }
        return {
            "ts": time.time(),
            "t": ir[R.IR["sim_time_s"]],
            "levels": levels,
            "flows": flows,
            "pumps": {
                "u1": hr[R.HR["pump1_permille"]],
                "u2": hr[R.HR["pump2_permille"]],
            },
            "mode": hr[R.HR["plc_mode"]],
            "sp": {"sp1": hr[R.HR["sp1_mm"]], "sp2": hr[R.HR["sp2_mm"]]},
            "pid": {
                "out1": hr[R.HR["pid1_out"]],
                "out2": hr[R.HR["pid2_out"]],
                "err1": R.from_signed16(hr[R.HR["pid1_err_mm"]]),
                "err2": R.from_signed16(hr[R.HR["pid2_err_mm"]]),
                "decouple": hr[R.HR["decouple_term"]],
            },
            "plc": {
                "heartbeat": hr[R.HR["plc_heartbeat"]],
                "scan_ms": hr[R.HR["plc_scan_ms"]],
                "online": self._plc_online(hr[R.HR["plc_heartbeat"]]),
            },
            "alarm_word": hr[R.HR["plc_alarm_word"]],
            "overflow": ir[R.IR["overflow"]],
            "alarms": self._alarm_list(hr[R.HR["plc_alarm_word"]]),
        }

    def _plc_online(self, hb: int) -> bool:
        now = time.time()
        if self.last_hb is None or hb != self.last_hb:
            self.last_hb = hb
            self.last_hb_change = now
            return True
        return (now - self.last_hb_change) < PLC_TIMEOUT_S

    def _alarm_list(self, word: int) -> list[dict[str, Any]]:
        out = []
        if not self.latest.get("plc", {}).get("online", True):
            out.append(
                {"code": "plc_comm_lost", "level": 1, "message": R.ALARM_LABELS["plc_comm_lost"]}
            )
        if self.sensor_fault:
            out.append(
                {"code": "sensor_fault", "level": 1, "message": R.ALARM_LABELS["sensor_fault"]}
            )
        for code, bit in R.ALARM_BITS.items():
            if code in ("plc_comm_lost", "sensor_fault"):
                continue
            if word & bit:
                out.append(
                    {"code": code, "level": R.ALARM_LEVEL[code], "message": R.ALARM_LABELS[code]}
                )
        return out

    def _track_alarms(self, alarms: list[dict[str, Any]]) -> None:
        current = {a["code"] for a in alarms}
        for a in alarms:
            if a["code"] not in self.active_alarms:
                self.active_alarms[a["code"]] = int(time.time())
                self.store.add_alarm(a["code"], a["level"], a["message"])
                log.warning("报警产生: %s", a["message"])
        for code in list(self.active_alarms):
            if code not in current:
                del self.active_alarms[code]
                log.info("报警消除: %s", R.ALARM_LABELS.get(code, code))

    # -------------------------------------------------------------- 主循环
    async def run(self) -> None:
        next_hist = 0.0
        if self.client is None:
            self.client = AsyncModbusTcpClient(R.PLANT_HOST, port=R.PLANT_PORT, timeout=0.4)
        while True:
            tick = time.perf_counter()
            try:
                if not self.connected:
                    self.connected = await self.client.connect()
                    if self.connected:
                        log.info("已连接现场从站 %s:%d", R.PLANT_HOST, R.PLANT_PORT)

                if self.connected:
                    ir_rr = await self.client.read_input_registers(0, count=R.IR_SIZE, slave=0)
                    hr_rr = await self.client.read_holding_registers(0, count=R.HR_SIZE, slave=0)
                    if ir_rr.isError() or hr_rr.isError():
                        raise IOError(f"读异常 ir={ir_rr} hr={hr_rr}")

                    snapshot = self._decode(ir_rr.registers, hr_rr.registers)
                    self.sensor_fault = any(
                        v > R.H_FULL_MM + 20 for v in snapshot["levels"].values()
                    )
                    self._track_alarms(snapshot["alarms"])
                    self.latest = snapshot

                    if time.time() >= next_hist:
                        self.store.add_sample(snapshot)
                        next_hist = time.time() + HISTORY_S
                else:
                    self.latest = {**self.latest, "online": False} if self.latest else {}
            except Exception as exc:  # noqa: BLE001 - 采集链路任何异常都不应中断服务
                self.connected = False
                if self.client is not None:
                    self.client.close()
                log.warning("采集异常: %s", exc)
                if self.latest:
                    self.latest = {**self.latest, "plc": {**self.latest.get("plc", {}), "online": False}}

            if self.latest:
                await self.broadcast(self.latest)

            elapsed = time.perf_counter() - tick
            await asyncio.sleep(max(0.0, POLL_S - elapsed))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.clients:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # -------------------------------------------------------------- 命令
    async def write_hr(self, address: int, value: int) -> None:
        if not self.connected:
            raise HTTPException(503, "现场通讯中断, 无法下发指令")
        rr = await self.client.write_register(address, int(value), slave=0)
        if rr.isError():
            raise HTTPException(502, f"写入失败: {rr}")

    async def shutdown(self) -> None:
        if self.client is not None:
            self.client.close()
        self.store.conn.close()


svc = ScadaService()


# ------------------------------------------------------------------ API
class Command(BaseModel):
    mode: int | None = Field(default=None, ge=0, le=1)
    sp1: int | None = Field(default=None, ge=20, le=320)
    sp2: int | None = Field(default=None, ge=20, le=320)
    pump1: int | None = Field(default=None, ge=0, le=1000)
    pump2: int | None = Field(default=None, ge=0, le=1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("SCADA 启动, 数据库 %s", DB_PATH)
    task = asyncio.create_task(svc.run())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await svc.shutdown()


app = FastAPI(title="三容水箱 SCADA", lifespan=lifespan)


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return svc.latest or {"online": False}


@app.get("/api/history")
async def get_history(seconds: float = 300, limit: int = 4000) -> list[dict[str, Any]]:
    seconds = max(10.0, min(seconds, 86400.0))
    return svc.store.recent(seconds, limit)


@app.get("/api/alarms")
async def get_alarms(limit: int = 50) -> list[dict[str, Any]]:
    return svc.store.recent_alarms(limit)


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "plant": {"host": R.PLANT_HOST, "port": R.PLANT_PORT},
        "tank_height_mm": R.H_FULL_MM,
        "alarm_labels": R.ALARM_LABELS,
    }


@app.post("/api/command")
async def post_command(cmd: Command) -> dict[str, Any]:
    applied: list[str] = []
    if cmd.sp1 is not None:
        await svc.write_hr(R.HR["sp1_mm"], cmd.sp1)
        applied.append(f"sp1={cmd.sp1}")
    if cmd.sp2 is not None:
        await svc.write_hr(R.HR["sp2_mm"], cmd.sp2)
        applied.append(f"sp2={cmd.sp2}")
    if cmd.pump1 is not None:
        await svc.write_hr(R.HR["pump1_permille"], cmd.pump1)
        applied.append(f"pump1={cmd.pump1}")
    if cmd.pump2 is not None:
        await svc.write_hr(R.HR["pump2_permille"], cmd.pump2)
        applied.append(f"pump2={cmd.pump2}")
    if cmd.mode is not None:
        await svc.write_hr(R.HR["plc_mode"], cmd.mode)
        applied.append(f"mode={cmd.mode}")
    log.info("操作员指令: %s", ", ".join(applied))
    return {"ok": True, "applied": applied}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    svc.clients.add(ws)
    try:
        if svc.latest:
            await ws.send_text(json.dumps(svc.latest, ensure_ascii=False))
        while True:
            await ws.receive_text()  # 前端心跳/保活, 内容忽略
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        svc.clients.discard(ws)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
