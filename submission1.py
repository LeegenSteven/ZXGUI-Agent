"""
决赛 GUI Agent（优化版）

相对你原 agent.py 的关键改动：
1. 安全护栏收窄：只在“真实扣款/付款”这一步前停手，指令明确要求的
   发布评论 / 收藏 / 加入书单 / 新建歌单 / 开始导航·播放 等都真正点完成。
2. 补回 Notetaker 跨屏记忆（Task Notes），解决“豆瓣前五→QQ音乐建歌单”这类
   需要跨 App 记住名字/排名/价格/时间 的任务。
3. 修复 TYPE：去掉无效的 \\n 提交约定，输入后单独点“搜索”提交；归一化时清掉尾部换行。
4. 修复图像管线：只做一次缩放，不再被基类 _encode_image 二次封顶抵消。
5. 代码层反重复兜底：连续重复同一动作时升级提示，达到阈值强制 BACK/SCROLL。
6. 精简点击推理契约，降低 output token。
保留：AST 动作解析（utils.action_parser）、TargetBox→点击中心纠偏、解析失败兜底为 WAIT。


不足：
1.B站赵丽颖视频筛选后没有播放，但是prompt里明确要求了要筛选播放
2.打开高德地图之后本来应该是上海虹桥火车站输入之后，再输入外滩。但是模型直接就输出了“从上海虹桥火车站导航到外滩”，然后再从高德的提示中选择了起始点与终点。这应该是可以接受的，但获取可以更好？
然后高德还有一个严重的问题是，没有识别到换成最少其实在页面中是有这个选项的，只不过没有左右滑动标签栏，我认为这里是否是模型的能力边界问题。是模型真不知道还是此处prompt左右了它的思考，看看有没有其他prompt可以成功让它识别
3.得到是把书加进了喜爱收藏，而不是我的书单。这个是目前最复杂的。
4.豆瓣选音乐的那里是直接搜的音乐排行榜，而不是书影音，模型这里是如何想的。
这里面是否可以增加一些泛化性的prompt进行引导模型的思考呢？
"""

import base64
import io
import json
import logging
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageFilter

from agent_base import (
    ACTION_BACK,
    ACTION_CLICK,
    ACTION_COMPLETE,
    ACTION_DOUBLE_CLICK,
    ACTION_DRAG,
    ACTION_HOME,
    ACTION_LONG_PRESS,
    ACTION_OPEN,
    ACTION_SCROLL,
    ACTION_TYPE,
    ACTION_WAIT,
    AgentInput,
    AgentOutput,
    BaseAgent,
    UsageInfo,
    VALID_ACTIONS,
)
from utils.action_parser import parse_model_output

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a mobile GUI agent. Given a task, compact memory, and the current phone screenshot, output exactly ONE next action.

## Output Format (plain text)
Thought:
- Progress: 已完成了什么 / 还差什么（用中文，1~2 句）。
- Next: 这一步最小目标是什么、点哪个元素（用中文，一句话）。
Action: <one action call>

For a CLICK / LONG_PRESS / DOUBLE_CLICK, the last lines MUST be:
TargetBox: [x1,y1,x2,y2]
Action: click(point='<point>x y</point>')   # point 必须落在 TargetBox 内部（取中心）

If the current screen contains information you will need AFTER navigating away
(例如排行榜前 N 的歌名、价格、时间、订单号等需要跨页/跨 App 复用的事实)，
add one or more lines, keeping names/numbers EXACT:
Note: <一条简洁事实>

## Action Space
click(point='<point>x y</point>')
long_press(point='<point>x y</point>', duration='1000')
double_click(point='<point>x y</point>')
type(content='要输入的文本')          # 只输入文本本身，不要在末尾加换行；提交搜索请下一步单独点“搜索/Search”按钮，若没有搜索按钮则TYPE("\n") 来代替。
scroll(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
scroll(point='<point>x y</point>', direction='down|up|left|right')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
open(app_name='应用中文名')
back()
home()
wait(seconds='2')
complete(content='done')

## Core Rules
- 坐标统一归一化到 [0,1000]：x=0 左、x=1000 右、y=0 上、y=1000 下，(500,500) 为屏幕中心。
- 以“当前截图”为唯一事实来源；记忆只用于追踪进度、避免重复、保存跨屏事实。
- 首屏通常是桌面。若目标 App 未打开，第一步用 open(app_name='指令中的应用名')。
- 多 App 任务（如先豆瓣后 QQ音乐）：完成前一个 App 的取数后，用 open() 切到下一个 App。
- 若 Task Notes 里有已记录的名字/排名/价格等事实，请按其“原样”使用，不要凭外部知识改写或补全。
- 输入文本前必须先点中输入框；只有当截图显示输入框已聚焦（出现软键盘/光标）时才用 type。
- 查询/搜索类：优先用搜索框输入目标词，再点“搜索”提交，不要去点历史词、热搜、推荐位、分类。
- 导航标签只切换板块，不满足“时间/时长/价格/距离/排序/最新”等筛选条件；这类条件要点对应的筛选/排序控件。
- 若上一步动作后界面没有变化，换一种方式（换目标、滚动、返回），不要原地重复点。

## Side-effect Policy（务必按此判断何时停止）
- 指令明确要求的动作就是任务目标，必须真正点击完成，包括：搜索、播放、筛选、排序、
  发布/发送评论、点赞收藏、加入书单、新建歌单、开始导航等。完成后再 complete(content='done')。
- 仅在“会真实扣款/下单付款”的最后一步之前停手：立即支付 / 确认支付 / 确认付款 /
  立即付款 / 提交订单(并付款) / 确认下单 / 立即购买 / 确认购买 / 去结算并付款 等。
  对这类付费任务，把流程推进到“订单确认页 / 支付确认页”即视为完成，不要点最终付款按钮，直接 complete。
- 除“真实付款”外，不要提前 complete。
"""


class Agent(BaseAgent):
    """单模型 GUI Agent：紧凑记忆 + Notetaker + reflection + 反重复兜底。"""

    # 记忆 / 截图配置
    MAX_RECENT_VISUAL_TURNS = 1        # 回看上一张截图用于 reflection
    MAX_RECENT_TEXT_STEPS = 8
    MAX_COMPRESSED_LINES = 12
    MAX_THOUGHT_CHARS = 160
    MAX_RAW_OUTPUT_CHARS = 600
    MAX_TASK_NOTES = 24

    # 图像配置（只做一次缩放）
    MODEL_IMAGE_LONG_SIDE = 1400       # 当前截图长边目标
    MODEL_IMAGE_MIN_SHORT_SIDE = 980   # 短边过小则放大并锐化（小屏小图标更清晰）
    MODEL_IMAGE_MAX_SCALE = 2.0
    PREV_IMAGE_LONG_SIDE = 900         # 上一张图压更小，省 token
    JPEG_QUALITY = 88

    ENABLE_TARGET_BOX_REPAIR = True

    # 反重复
    REPEAT_WARN_THRESHOLD = 2          # 连续重复 >=2 次：强提示
    REPEAT_FORCE_THRESHOLD = 3         # 连续重复 >=3 次：强制兜底动作

    # ---------- 生命周期 ----------

    def _initialize(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._recent_visual_turns: List[Dict[str, Any]] = []
        self._recent_text_steps: List[Dict[str, Any]] = []
        self._compressed_steps: List[str] = []
        self._task_notes: List[str] = []
        self._step_index = 0
        self._last_instruction: Optional[str] = None
        self._parse_fail_count = 0
        self._action_sig_history: deque = deque(maxlen=6)
        self._consecutive_repeat = 0
        self._recovery_toggle = 0
        logger.info("Agent state reset")

    # ---------- Prompt 构建 ----------

    def _build_system_prompt(self, instruction: str) -> str:
        return f"{SYSTEM_PROMPT}\n\n## User Instruction\n{instruction}\n"

    def _build_instruction_hint(self, image: Image.Image) -> str:
        w, h = image.size
        return (
            "## Coordinate Guide\n"
            f"Screenshot pixels: {w}x{h}. Output coordinates normalized to [0,1000].\n"
            "Anchors: top-left=(0,0), center=(500,500), bottom-right=(1000,1000). "
            "竖直：顶栏 y<150，内容区 y≈150~850，底部导航/输入区 y>850。"
        )

    def _build_memory_text(self, input_data: AgentInput) -> str:
        lines = ["## Compact Memory", f"Current step: {input_data.step_count}"]

        if self._task_notes:
            lines.append("\n### Task Notes（已记录事实，按原样使用，勿改写）")
            lines.extend(f"- {note}" for note in self._task_notes[-self.MAX_TASK_NOTES:])

        if self._compressed_steps:
            lines.append("\n### Older Progress Summary")
            lines.extend(f"- {item}" for item in self._compressed_steps[-self.MAX_COMPRESSED_LINES:])

        if self._recent_text_steps:
            lines.append("\n### Recent Steps")
            for step in self._recent_text_steps[-self.MAX_RECENT_TEXT_STEPS:]:
                thought = self._shorten(step.get("thought") or step.get("reflection") or "", self.MAX_THOUGHT_CHARS)
                params = self._format_parameters(step.get("parameters", {}))
                lines.append(f"- Step {step.get('step')}: {thought} => {step.get('action')} {params}")
        else:
            lines.append("\n### Recent Steps\n- None. 这是该任务的第一步决策。")

        warn = self._build_repeat_warning()
        if warn:
            lines.append(warn)

        if self._parse_fail_count:
            lines.append(
                f"\n### Warning\n- 之前解析失败 {self._parse_fail_count} 次。"
                "请只输出一行合法的 Action（含 point/参数）。"
            )

        lines.append(
            "\n### Decision Reminder\n"
            "只有当前截图能直接证明某个子目标已完成时才算完成；"
            "不要仅凭记忆推断聚焦状态、已输入内容、选择结果或任务完成。"
        )
        return "\n".join(lines)

    def _build_repeat_warning(self) -> str:
        if self._consecutive_repeat >= self.REPEAT_FORCE_THRESHOLD:
            return (
                "\n### Stuck Warning\n"
                f"- 你已连续 {self._consecutive_repeat} 次重复同一动作且界面无效果。"
                "必须换一种策略：换点击目标 / 滚动寻找 / back 返回上一层。"
            )
        if self._consecutive_repeat >= self.REPEAT_WARN_THRESHOLD:
            return (
                "\n### Repeat Warning\n"
                f"- 你已连续 {self._consecutive_repeat} 次重复同一动作。"
                "请确认它是否真的生效；若界面没变化，请改用其它动作。"
            )
        return ""

    def generate_messages(self, input_data: AgentInput) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt(input_data.instruction)},
            {"role": "user", "content": self._build_memory_text(input_data)},
        ]

        # reflection：回看上一张截图 + 上一步原始输出
        for turn in self._recent_visual_turns[-self.MAX_RECENT_VISUAL_TURNS:]:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"上一步(Step {turn.get('step')})执行前的截图，用于判断上一步是否生效："},
                        {"type": "image_url", "image_url": {"url": turn["image_url"]}},
                    ],
                }
            )
            raw = self._shorten(turn.get("raw_output", ""), self.MAX_RAW_OUTPUT_CHARS)
            if raw:
                messages.append({"role": "assistant", "content": raw})

        messages.append({"role": "user", "content": self._build_instruction_hint(input_data.current_image)})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "当前截图。请基于这一屏决定唯一的下一步动作："},
                    {"type": "image_url", "image_url": {"url": self._encode_model_image(input_data.current_image)}},
                ],
            }
        )
        return messages

    # ---------- 图像编码（只缩放一次，避免被基类二次封顶） ----------

    def _resize_for_model(self, image: Image.Image, long_side: int, min_short: int = 0) -> Image.Image:
        w, h = image.size
        img = image
        long_now = max(w, h)
        if long_now > long_side:
            r = long_side / long_now
            img = img.resize((max(1, int(round(w * r))), max(1, int(round(h * r)))), Image.LANCZOS)
        elif min_short:
            short_now = min(w, h)
            if 0 < short_now < min_short:
                r = min(min_short / short_now, self.MODEL_IMAGE_MAX_SCALE)
                if r > 1.0:
                    img = img.resize((max(1, int(round(w * r))), max(1, int(round(h * r)))), Image.LANCZOS)
                    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80, threshold=2))
        return img

    def _to_base64_jpeg(self, image: Image.Image) -> str:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=self.JPEG_QUALITY)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

    def _encode_model_image(self, image: Image.Image) -> str:
        return self._to_base64_jpeg(
            self._resize_for_model(image, self.MODEL_IMAGE_LONG_SIDE, self.MODEL_IMAGE_MIN_SHORT_SIDE)
        )

    def _encode_prev_image(self, image: Image.Image) -> str:
        return self._to_base64_jpeg(self._resize_for_model(image, self.PREV_IMAGE_LONG_SIDE))

    # ---------- 小工具 ----------

    def _shorten(self, text: str, max_chars: int) -> str:
        if not text:
            return ""
        compact = " ".join(str(text).split())
        return compact if len(compact) <= max_chars else compact[: max_chars - 3] + "..."

    def _format_parameters(self, parameters: Dict[str, Any]) -> str:
        if not parameters:
            return "{}"
        return "{" + ", ".join(f"{k}={v}" for k, v in parameters.items()) + "}"

    def _normalize_xy(self, x_raw: Any, y_raw: Any) -> Optional[List[int]]:
        try:
            x = float(x_raw)
            y = float(y_raw)
        except (TypeError, ValueError):
            return None
        if -1e-6 <= x <= 1.0 + 1e-6 and -1e-6 <= y <= 1.0 + 1e-6:
            x *= 1000.0
            y *= 1000.0
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return None
        return [int(round(x)), int(round(y))]

    # ---------- Notetaker ----------

    def _extract_notes(self, raw_output: str) -> List[str]:
        if not raw_output:
            return []
        notes = re.findall(r"^\s*Note\s*[:：]\s*(.+)$", raw_output, flags=re.MULTILINE | re.IGNORECASE)
        cleaned = []
        for n in notes:
            n = " ".join(n.split())
            if n and n.lower() not in ("xxx", "none", "n/a"):
                cleaned.append(n)
        return cleaned

    def _add_notes(self, notes: List[str]) -> None:
        for n in notes:
            if n not in self._task_notes:
                self._task_notes.append(n)
        if len(self._task_notes) > self.MAX_TASK_NOTES:
            self._task_notes = self._task_notes[-self.MAX_TASK_NOTES:]

    # ---------- TargetBox 纠偏 / 解析兜底 ----------

    def _extract_target_box(self, raw_output: str) -> Optional[List[int]]:
        patterns = [
            r"(?:TargetBox|BBox|BoundingBox|目标框|目标区域)\s*[:：]\s*[\[\(]\s*"
            r"(-?\d{1,4})\s*[,，]\s*(-?\d{1,4})\s*[,，]\s*(-?\d{1,4})\s*[,，]\s*(-?\d{1,4})\s*[\]\)]",
            r"(?:TargetBox|BBox|BoundingBox|目标框|目标区域)\s*[:：]\s*"
            r"(-?\d{1,4})\s+(-?\d{1,4})\s+(-?\d{1,4})\s+(-?\d{1,4})",
        ]
        for pattern in patterns:
            m = re.search(pattern, raw_output or "", re.IGNORECASE)
            if not m:
                continue
            x1, y1, x2, y2 = [int(v) for v in m.groups()]
            x1, x2 = sorted((max(0, min(1000, x1)), max(0, min(1000, x2))))
            y1, y2 = sorted((max(0, min(1000, y1)), max(0, min(1000, y2))))
            if 8 <= x2 - x1 <= 920 and 8 <= y2 - y1 <= 920:
                return [x1, y1, x2, y2]
        return None

    def _point_in_box(self, point: List[int], box: List[int], margin: int = 20) -> bool:
        x, y = point
        x1, y1, x2, y2 = box
        return (x1 - margin) <= x <= (x2 + margin) and (y1 - margin) <= y <= (y2 + margin)

    def _repair_click_coordinates(self, raw_output: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
        if not self.ENABLE_TARGET_BOX_REPAIR:
            return parsed
        if parsed.get("action") not in (ACTION_CLICK, ACTION_LONG_PRESS, ACTION_DOUBLE_CLICK):
            return parsed
        params = parsed.get("parameters") or {}
        point = params.get("point")
        if not (isinstance(point, list) and len(point) == 2):
            return parsed
        box = self._extract_target_box(raw_output)
        if box and not self._point_in_box([int(point[0]), int(point[1])], box):
            repaired = [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]
            logger.info(f"Repair CLICK by TargetBox: {point} -> {repaired}, box={box}")
            fixed = dict(parsed)
            fixed["parameters"] = dict(params)
            fixed["parameters"]["point"] = repaired
            return fixed
        return parsed

    def _recover_action_from_raw(self, raw_output: str) -> Optional[Dict[str, Any]]:
        if not raw_output:
            return None
        box = self._extract_target_box(raw_output)
        if box:
            center = [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]
            return {"action": ACTION_CLICK, "parameters": {"point": center}, "raw_action": "recovered_from_target_box"}
        m = re.search(
            r"click\s*\(\s*point\s*=\s*['\"]?\s*(?:<point>)?\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)",
            raw_output, re.IGNORECASE,
        )
        if m:
            point = self._normalize_xy(m.group(1), m.group(2))
            if point:
                return {"action": ACTION_CLICK, "parameters": {"point": point}, "raw_action": "recovered_click_point"}
        return None

    def _normalize_parsed_action(self, parsed: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not parsed:
            return None
        action = str(parsed.get("action") or "").upper()
        if action not in VALID_ACTIONS:
            return None
        params = parsed.get("parameters") or {}
        if not isinstance(params, dict):
            params = {}

        normalized: Dict[str, Any] = {}
        if action in (ACTION_CLICK, ACTION_DOUBLE_CLICK):
            p = params.get("point")
            if isinstance(p, list) and len(p) >= 2:
                norm = self._normalize_xy(p[0], p[1])
                if norm:
                    normalized["point"] = norm
        elif action == ACTION_LONG_PRESS:
            p = params.get("point")
            if isinstance(p, list) and len(p) >= 2:
                norm = self._normalize_xy(p[0], p[1])
                if norm:
                    normalized["point"] = norm
                    normalized["duration"] = int(params.get("duration", 1000) or 1000)
        elif action in (ACTION_SCROLL, ACTION_DRAG):
            s, e = params.get("start_point"), params.get("end_point")
            if isinstance(s, list) and isinstance(e, list) and len(s) >= 2 and len(e) >= 2:
                sn, en = self._normalize_xy(s[0], s[1]), self._normalize_xy(e[0], e[1])
                if sn and en:
                    normalized["start_point"] = sn
                    normalized["end_point"] = en
        elif action == ACTION_TYPE:
            # 关键修复：清掉尾部换行，避免污染搜索框 / 无效“提交”约定
            # normalized["text"] = str(params.get("text", "")).rstrip("\r\n")
            text = str(params.get("text", ""))
            if text and text.strip("\r\n") == "":
                normalized["text"] = "\n"          # 纯换行 -> 保留，触发新版控制器的回车提交
            else:
                normalized["text"] = text.rstrip("\r\n")   # 带内容 -> 只去尾随换行
        elif action == ACTION_OPEN:
            normalized["app_name"] = str(params.get("app_name", "")).strip()
        elif action == ACTION_WAIT:
            try:
                normalized["seconds"] = max(0, int(round(float(params.get("seconds", 2)))))
            except (TypeError, ValueError):
                normalized["seconds"] = 2
        elif action in (ACTION_BACK, ACTION_HOME, ACTION_COMPLETE):
            normalized = {}

        result = dict(parsed)
        result["action"] = action
        result["parameters"] = normalized
        return result

    def _is_valid_parsed_action(self, parsed: Optional[Dict[str, Any]]) -> bool:
        if not parsed:
            return False
        action, params = parsed.get("action"), parsed.get("parameters")
        if action not in VALID_ACTIONS or not isinstance(params, dict):
            return False
        if action in (ACTION_CLICK, ACTION_DOUBLE_CLICK, ACTION_LONG_PRESS):
            return isinstance(params.get("point"), list) and len(params["point"]) == 2
        if action in (ACTION_SCROLL, ACTION_DRAG):
            return (
                isinstance(params.get("start_point"), list) and len(params["start_point"]) == 2
                and isinstance(params.get("end_point"), list) and len(params["end_point"]) == 2
            )
        if action == ACTION_TYPE:
            return "text" in params
        if action == ACTION_OPEN:
            return bool(params.get("app_name"))
        if action == ACTION_WAIT:
            return "seconds" in params
        return action in (ACTION_BACK, ACTION_HOME, ACTION_COMPLETE)

    # ---------- 反重复 ----------

    def _action_signature(self, parsed: Dict[str, Any]) -> str:
        action = parsed.get("action", "")
        params = parsed.get("parameters", {}) or {}
        rounded: Dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, list) and len(v) == 2 and all(isinstance(n, (int, float)) for n in v):
                # 坐标量化到 ~3% 网格，容忍模型微小抖动
                rounded[k] = [round(v[0] / 30), round(v[1] / 30)]
            else:
                rounded[k] = v
        return f"{action}:{json.dumps(rounded, ensure_ascii=False, sort_keys=True)}"

    def _update_repeat_state(self, parsed: Dict[str, Any]) -> None:
        sig = self._action_signature(parsed)
        if self._action_sig_history and self._action_sig_history[-1] == sig:
            self._consecutive_repeat += 1
        else:
            self._consecutive_repeat = 1
        self._action_sig_history.append(sig)

    def _forced_recovery_action(self) -> Dict[str, Any]:
        """连续重复达到阈值时的兜底：交替 滚动 / 返回，避免整轮卡死。"""
        self._recovery_toggle += 1
        if self._recovery_toggle % 2 == 1:
            return {
                "action": ACTION_SCROLL,
                "parameters": {"start_point": [500, 700], "end_point": [500, 350]},
                "raw_action": "forced_recovery_scroll",
            }
        return {"action": ACTION_BACK, "parameters": {}, "raw_action": "forced_recovery_back"}

    # ---------- 记录 ----------

    def _record_step(self, input_data: AgentInput, raw_output: str, parsed: Dict[str, Any]) -> None:
        self._step_index += 1
        step_no = input_data.step_count or self._step_index
        record = {
            "step": step_no,
            "thought": parsed.get("thought", ""),
            "reflection": parsed.get("reflection", ""),
            "action": parsed.get("action", ""),
            "parameters": parsed.get("parameters", {}),
        }
        self._recent_text_steps.append(record)
        while len(self._recent_text_steps) > self.MAX_RECENT_TEXT_STEPS:
            old = self._recent_text_steps.pop(0)
            thought = self._shorten(old.get("thought") or old.get("reflection") or "", 70)
            self._compressed_steps.append(
                f"Step {old.get('step')}: {thought} => {old.get('action')} {self._format_parameters(old.get('parameters', {}))}"
            )
        self._compressed_steps = self._compressed_steps[-self.MAX_COMPRESSED_LINES:]

        self._recent_visual_turns.append(
            {"step": step_no, "image_url": self._encode_prev_image(input_data.current_image), "raw_output": raw_output}
        )
        self._recent_visual_turns = self._recent_visual_turns[-self.MAX_RECENT_VISUAL_TURNS:]

    # ---------- LLM ----------

    def _call_llm(self, messages: List[Dict[str, Any]]) -> Tuple[str, Optional[UsageInfo]]:
        try:
            response = self._call_api(messages=messages, temperature=0, top_p=0.7)
            content = (response.choices[0].message.content or "").strip()
            return content, self.extract_usage_info(response)
        except Exception as exc:
            logger.error(f"LLM request failed: {exc}", exc_info=True)
            return "", None

    # ---------- 主流程 ----------

    def act(self, input_data: AgentInput) -> AgentOutput:
        if self._last_instruction is not None and self._last_instruction != input_data.instruction:
            self.reset()
        self._last_instruction = input_data.instruction

        raw_output, usage = self._call_llm(self.generate_messages(input_data))
        if not raw_output:
            return AgentOutput(action=ACTION_WAIT, parameters={"seconds": 2}, raw_output="", usage=usage)

        logger.info(f"Model output: {raw_output}")

        # Notetaker：先收集本步要记住的事实
        self._add_notes(self._extract_notes(raw_output))

        parsed = self._normalize_parsed_action(parse_model_output(raw_output))
        if not self._is_valid_parsed_action(parsed):
            self._parse_fail_count += 1
            logger.warning("解析失败，尝试兜底恢复")
            parsed = self._normalize_parsed_action(self._recover_action_from_raw(raw_output))

        # 解析不出合法动作：兜底 WAIT（绝不误判为 COMPLETE）
        if not self._is_valid_parsed_action(parsed):
            logger.error("无法恢复合法动作，改为 WAIT")
            parsed = {"action": ACTION_WAIT, "parameters": {"seconds": 2}, "raw_action": "fallback_wait"}

        parsed = self._repair_click_coordinates(raw_output, parsed)
        parsed = self._normalize_parsed_action(parsed)
        if not self._is_valid_parsed_action(parsed):
            parsed = {"action": ACTION_WAIT, "parameters": {"seconds": 2}, "raw_action": "fallback_wait_after_repair"}

        # 反重复：连续重复同一动作且非 COMPLETE/WAIT 时强制兜底
        self._update_repeat_state(parsed)
        if (
            self._consecutive_repeat >= self.REPEAT_FORCE_THRESHOLD
            and parsed.get("action") not in (ACTION_COMPLETE, ACTION_WAIT)
        ):
            logger.warning(f"连续重复 {self._consecutive_repeat} 次，强制恢复动作")
            parsed = self._forced_recovery_action()
            self._consecutive_repeat = 0
            self._action_sig_history.clear()

        self._record_step(input_data, raw_output, parsed)
        return AgentOutput(
            action=parsed["action"],
            parameters=parsed["parameters"],
            raw_output=raw_output,
            usage=usage,
        )