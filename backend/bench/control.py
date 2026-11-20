"""机器人状态切换：阻尼、IKStand（准备站立）、Walk、坐下、躺下、起身。

SDK 指令和成功判定照搬 oli-robot-manager（network/mcp_client.py、SDK按钮逻辑与joystick映射说明.md）：
- 阻尼 request_damping；IKStand request_prepare；Walk request_set_walk_mode（已在 Walk 返回
  fail_state_not_allowed + current_state=Walk 也算成功）；坐下 request_from_stand_to_sit；躺下 request_lie_down；
- 起身 request_standup {"mode": sitting/lying/hanging}，成功后接着 request_set_walk_mode，两步都成功才算成功。
- 阻尼会让全身停止主动发力，默认锁住；吊装保护打开时不能坐下、躺下，起身用 hanging。
"""
from __future__ import annotations

import time

ACTIONS = {
    "damping": {"label": "阻尼", "steps": (("request_damping", {}),), "danger": True},
    "prepare": {"label": "IKStand", "steps": (("request_prepare", {}),)},
    "walk": {"label": "Walk", "steps": (("request_set_walk_mode", {}),)},
    "sit": {"label": "坐下", "steps": (("request_from_stand_to_sit", {}),), "no_hanging": True},
    "lie": {"label": "躺下", "steps": (("request_lie_down", {}),), "no_hanging": True},
    "stand_sit": {"label": "坐姿起身", "steps": (("request_standup", {"mode": "sitting"}), ("request_set_walk_mode", {})), "no_hanging": True},
    "stand_lie": {"label": "躺姿起身", "steps": (("request_standup", {"mode": "lying"}), ("request_set_walk_mode", {})), "no_hanging": True},
    "stand_hang": {"label": "悬吊起身", "steps": (("request_standup", {"mode": "hanging"}), ("request_set_walk_mode", {})), "hanging_only": True},
}
REASON = {
    "fail_state_not_allowed": "当前状态不允许这个切换",
    "fail_timeout": "机器人执行超时",
    "fail_invalid_param": "参数不对",
}


def ok(title: str, data: dict) -> bool:
    if data.get("result") == "success":
        return True
    return title == "request_set_walk_mode" and data.get("result") == "fail_state_not_allowed" and data.get("current_state") == "Walk"


def public() -> list[dict]:
    return [{"key": k, "label": v["label"], "danger": bool(v.get("danger")), "no_hanging": bool(v.get("no_hanging")),
             "hanging_only": bool(v.get("hanging_only"))} for k, v in ACTIONS.items()]


async def run(robot, key: str, *, hanging: bool, unlocked: bool) -> dict:
    """按顺序发指令，任何一步失败就停，不再发后面的。返回 {ok, text, steps}。"""
    a = ACTIONS.get(key)
    if not a:
        raise ValueError("没有这个切换")
    if a.get("danger") and not unlocked:
        raise ValueError("阻尼会让机器人全身无力：先勾选“允许阻尼”，再点")
    if hanging and a.get("no_hanging"):
        raise ValueError(f"吊装保护开着，不能{a['label']}；起身用“悬吊起身”")
    if not hanging and a.get("hanging_only"):
        raise ValueError("悬吊起身只在吊装保护打开时用")
    steps = []
    t0 = time.time()
    for title, data in a["steps"]:
        res = await robot.request(title, data, timeout=60)
        steps.append({"title": title, "data": data, "result": res})
        if not ok(title, res):
            why = REASON.get(res.get("result", ""), res.get("result", "没有结果"))
            cur = res.get("current_state")
            return {"ok": False, "steps": steps,
                    "text": f"{a['label']}失败：{why}" + (f"（机器人现在是 {cur}）" if cur else "")}
    return {"ok": True, "steps": steps, "text": f"{a['label']}完成，用时 {time.time() - t0:.1f} s"}
