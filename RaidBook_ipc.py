# -*- coding: utf-8 -*-
import asyncio
import inspect
import json


async def send_ipc(port, cmd, args=None, *, timeout_connect=5, timeout_read=15):
    if not port:
        return {"ok": False, "error": "IPC 포트가 설정되지 않았어요."}
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", int(port)),
            timeout=timeout_connect,
        )
        payload = json.dumps({"cmd": cmd, "args": args or []}, ensure_ascii=False)
        writer.write(payload.encode("utf-8") + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=timeout_read)
        return json.loads(data.decode("utf-8"))
    except asyncio.TimeoutError:
        return {"ok": False, "error": "응답 타임아웃 (봇이 바쁘거나 오프라인)"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "봇 IPC 연결 실패 (오프라인이거나 시작 중)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def write_json_response(writer, response):
    try:
        writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def dispatch_ipc_command(command_map, cmd_name, args=None, *, fallback=None):
    handler = command_map.get(cmd_name)
    if handler is None:
        if fallback is None:
            return {"ok": False, "error": f"명령어 없음: {cmd_name}"}
        result = fallback(cmd_name, args or [])
    else:
        result = handler(args or [])
    if inspect.isawaitable(result):
        result = await result
    return result
