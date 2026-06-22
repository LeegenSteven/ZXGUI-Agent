"""
Action parser for the final-round GUI agent.

It accepts UI-TARS style model output and returns the standard action shape
expected by the runner:
    {"action": "CLICK", "parameters": {"point": [500, 300]}}
"""

import ast
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COORD_MIN = 0
COORD_MAX = 1000

ACTION_NAME_MAP = {
    "click": "CLICK",
    "tap": "CLICK",
    "left_single": "CLICK",
    "long_press": "LONG_PRESS",
    "double_click": "DOUBLE_CLICK",
    "left_double": "DOUBLE_CLICK",
    "drag": "DRAG",
    "swipe": "SCROLL",
    "scroll": "SCROLL",
    "type": "TYPE",
    "input": "TYPE",
    "open": "OPEN",
    "open_app": "OPEN",
    "back": "BACK",
    "home": "HOME",
    "wait": "WAIT",
    "complete": "COMPLETE",
    "finished": "COMPLETE",
    "finish": "COMPLETE",
    "done": "COMPLETE",
}

CALL_NAMES = "|".join(sorted(ACTION_NAME_MAP, key=len, reverse=True))


def _clamp(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0
    if -1e-6 <= number <= 1.0 + 1e-6:
        number *= 1000.0
    return max(COORD_MIN, min(COORD_MAX, int(round(number))))


def _normalize_point(point: Any) -> Optional[List[int]]:
    if isinstance(point, dict):
        point = [point.get("x"), point.get("y")]
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return [_clamp(point[0]), _clamp(point[1])]
    if isinstance(point, str):
        points = extract_point_coordinates(point)
        if points:
            return points[0]
        nums = re.findall(r"-?\d+(?:\.\d+)?", point)
        if len(nums) >= 2:
            return [_clamp(nums[0]), _clamp(nums[1])]
    return None


def _point_from_box(box: Any) -> Optional[List[int]]:
    if isinstance(box, str):
        nums = re.findall(r"-?\d+(?:\.\d+)?", box)
        box = nums
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        x1, y1, x2, y2 = [_clamp(v) for v in box[:4]]
        return [(x1 + x2) // 2, (y1 + y2) // 2]
    return None


def extract_point_coordinates(text: str) -> List[List[int]]:
    """Extract all <point>x y</point> coordinates."""
    matches = re.findall(
        r"<point>\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*</point>",
        str(text),
        flags=re.IGNORECASE,
    )
    return [[_clamp(x), _clamp(y)] for x, y in matches]


def parse_action_string(action_str: str) -> Optional[Dict[str, Any]]:
    """Parse one Python-like function call into name and keyword args."""
    try:
        node = ast.parse(action_str.strip(), mode="eval")
        call = node.body
        if not isinstance(call, ast.Call):
            return None
        if isinstance(call.func, ast.Name):
            func_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            func_name = call.func.attr
        else:
            return None

        args: Dict[str, Any] = {}
        for index, arg in enumerate(call.args):
            try:
                args[f"arg{index}"] = ast.literal_eval(arg)
            except Exception:
                args[f"arg{index}"] = None
        for kw in call.keywords:
            if kw.arg is None:
                continue
            try:
                args[kw.arg] = ast.literal_eval(kw.value)
            except Exception:
                args[kw.arg] = None
        return {"function": func_name, "args": args}
    except Exception:
        return None


def extract_action_call(text: str) -> Optional[str]:
    """Return the first complete action call after the last Action: marker."""
    if not text:
        return None
    region = str(text).rsplit("Action:", 1)[-1] if "Action:" in str(text) else str(text)
    pattern = re.compile(
        rf"\b(?:{CALL_NAMES})\s*"
        r"\((?:[^()'\"\\]|\\.|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")*\)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(region)
    return match.group(0).strip() if match else None


def _scroll_from_point_direction(point: List[int], direction: str) -> Dict[str, List[int]]:
    x, y = point
    distance = 250
    direction = (direction or "down").lower()
    if direction == "up":
        end = [x, max(COORD_MIN, y - distance)]
    elif direction == "left":
        end = [max(COORD_MIN, x - distance), y]
    elif direction == "right":
        end = [min(COORD_MAX, x + distance), y]
    else:
        end = [x, min(COORD_MAX, y + distance)]
    return {"start_point": point, "end_point": end}


def parse_model_output(text: str) -> Optional[Dict[str, Any]]:
    """Parse model output into the runner's standard action dictionary."""
    if not text:
        return None
    raw_text = str(text).strip()
    action_call = extract_action_call(raw_text)
    if not action_call:
        return None

    parsed = parse_action_string(action_call)
    if not parsed:
        return None

    action = ACTION_NAME_MAP.get(parsed["function"].lower(), parsed["function"].upper())
    args = parsed["args"]
    params: Dict[str, Any] = {}

    if action == "CLICK":
        point = (
            _normalize_point(args.get("point"))
            or _normalize_point(args.get("coord"))
            or _normalize_point(args.get("coordinates"))
            or _point_from_box(args.get("box"))
            or _point_from_box(args.get("bbox"))
            or _point_from_box(args.get("start_box"))
        )
        if point is None:
            return None
        params["point"] = point

    elif action == "LONG_PRESS":
        point = _normalize_point(args.get("point")) or _point_from_box(args.get("box"))
        if point is None:
            return None
        params["point"] = point
        try:
            params["duration"] = int(float(args.get("duration", 1000)))
        except (TypeError, ValueError):
            params["duration"] = 1000

    elif action == "DOUBLE_CLICK":
        point = _normalize_point(args.get("point")) or _point_from_box(args.get("box"))
        if point is None:
            return None
        params["point"] = point

    elif action in ("SCROLL", "DRAG"):
        start = (
            _normalize_point(args.get("start_point"))
            or _normalize_point(args.get("start"))
            or _normalize_point(args.get("from"))
            or _point_from_box(args.get("start_box"))
        )
        end = (
            _normalize_point(args.get("end_point"))
            or _normalize_point(args.get("end"))
            or _normalize_point(args.get("to"))
            or _point_from_box(args.get("end_box"))
        )
        if start and end:
            params["start_point"] = start
            params["end_point"] = end
        else:
            point = _normalize_point(args.get("point"))
            if not point:
                return None
            params.update(_scroll_from_point_direction(point, str(args.get("direction", "down"))))

    elif action == "TYPE":
        params["text"] = str(args.get("content") if "content" in args else args.get("text", ""))

    elif action == "OPEN":
        params["app_name"] = str(args.get("app_name") if "app_name" in args else args.get("app", ""))

    elif action == "WAIT":
        try:
            seconds = float(args.get("seconds", args.get("duration", 2)))
        except (TypeError, ValueError):
            seconds = 2
        params["seconds"] = max(0, int(round(seconds)))

    elif action in ("BACK", "HOME", "COMPLETE"):
        params = {}

    else:
        return None

    return {
        "action": action,
        "parameters": params,
        "thought": _extract_block(raw_text, "Thought"),
        "reflection": _extract_block(raw_text, "Reflection"),
        "raw_action": action_call,
    }


def _extract_block(text: str, key: str) -> str:
    pattern = rf"{key}\s*:\s*(.+?)(?=\n\s*(?:Action|TargetBox|Reflection|Thought)\s*:|$)"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""
