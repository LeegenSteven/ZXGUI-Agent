"""
决赛 GUI Agent(终版)

本次相对上一版的改动,聚焦在"教能力"而不是"教文案":

- 删除上一版里所有"得到 App 特定描述"(+新建清单 / 加入学习清单 / 我的-收藏)。
- 改用三条 App-agnostic 的通用原则：

  (1) 输出格式新增 LastEffect 字段:强制每一步先用客观证据描述上一步效果,
      再做 Progress 判定。这是反 confirmation bias 的主要手段。

  (2) Verify Each Step:任务里的关键词(具名容器名/具体动作/数量/筛选条件)
      必须在当前截图里找到逐字对应的证据,才能判定子目标完成。"类似但不一致"
      的 UI 反馈(如任务要"加入<容器>"但截图只显示"已收藏")一律判未完成。
      这一条用抽象陷阱模式而非具体 App 文案表达。

  (3) Two-Step Pattern:把"主按钮 → 二级选择/确认面板 → 底部按钮"这个抽象
      UI 模式作为通用知识传给模型。这个模式适用于书单/歌单/收藏夹/分享对话框/
      购物车选规格/给联系人打标签等几乎所有 App,不依赖具体 UI 文案。

  (4) Counting Tasks:对"N 个/N 首/N 条"任务,强制 Progress 写 K/N done,
      K < N 不许 complete。

其余 monkey patch / Notetaker / 反重复 / TargetBox 纠偏等逻辑保持原样。
"""

import base64
import io
import json
import logging
import re
import shlex
import time
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


# ============================================================
# DeviceController.input_text 补丁(逻辑保持不变)
# ============================================================
_PRE_TAP_DELAY = 0.30
_PRE_TAP_TOP_Y_MAX = 200

_pre_tap_state: Dict[str, Any] = {"point": [500, 122], "enabled": True}



# ============================================================
SYSTEM_PROMPT = """You are a mobile GUI agent. Given a task, compact memory, and the current phone screenshot, output exactly ONE next action.

## Output Format (plain text)
Thought:
- LastEffect: 上一步预期看到什么,当前截图里实际可见的证据是什么,上一步是
  生效 / 未生效 / 部分生效。**只能基于当前截图客观陈述**(像描述一张照片那样),
  不要用"应该/已经/想必/估计"这类推断词。第一步写 "N/A"。
- Progress: 任务整体进展。对于"N 个/N 首/N 条"等计数任务,**必须写明"K/N done"**,
  其中每个 K 都对应一个可在 Recent Steps 或当前截图里找到证据的已完成 item。
  写出"还差什么"。
- Next: 这一步要做的最小动作,中文一句话,写出点哪个元素。
Action: <one action call>

For a CLICK / LONG_PRESS / DOUBLE_CLICK, the last lines MUST be:
TargetBox: [x1,y1,x2,y2]
Action: click(point='<point>x y</point>')   # point 必须落在 TargetBox 内部(取中心)

If the current screen contains information you will need AFTER navigating away
(例如排行榜前 N 的歌名、价格、时间、订单号、已新建的容器名等需要跨页/跨 App
复用的事实),add one or more lines, keeping names/numbers EXACT:
Note: <一条简洁事实>

## Action Space
click(point='<point>x y</point>')
long_press(point='<point>x y</point>', duration='1000')
double_click(point='<point>x y</point>')
type(content='要输入的文本')          # 只输入文本本身,不要在末尾加换行;提交搜索请下一步单独点"搜索/Search"按钮,若没有搜索按钮则 type("\\n") 代替。
scroll(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
scroll(point='<point>x y</point>', direction='down|up|left|right')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
open(app_name='应用中文名')
back()
home()
wait(seconds='2')
complete(content='done')

## Core Rules
- 坐标统一归一化到 [0,1000]:x=0 左、x=1000 右、y=0 上、y=1000 下,(500,500) 为屏幕中心。
- 以"当前截图"为唯一事实来源;记忆只用于追踪进度、避免重复、保存跨屏事实。
- 首屏通常是桌面。若目标 App 未打开,第一步用 open(app_name='指令中的应用名')。
- 多 App 任务(如先豆瓣后 QQ音乐):完成前一个 App 的取数后,用 open() 切到下一个 App。
- 若 Task Notes 里有已记录的名字/排名/价格等事实,请按其"原样"使用,不要凭外部知识改写或补全。
- 输入文本前必须先点中输入框;只有当截图显示输入框已聚焦(出现软键盘/光标)时才用 type。
- 输入内容时只填写歌名/地名/关键词本身,不要加上作者、演唱者等附加信息。
- 查询/搜索类:优先用搜索框输入目标词,再点"搜索"提交,不要去点历史词、热搜、推荐位、分类。
- 导航标签只切换板块,不满足"时间/时长/价格/距离/排序/最新"等筛选条件;这类条件要点对应的筛选/排序控件。
- 若上一步动作后界面没有变化,换一种方式(换目标、滚动、返回),不要原地重复点。
- 搜索框是"最后手段",不是"第一反应"。在点击任何搜索图标之前,必须先在截图中逐项扫描以下位置,确认没有匹配入口:
    (1) 底部导航栏或顶部标签栏的所有标签
    (2) 主内容区的功能卡片/版块入口
    注意一定只有当以上位置扫描后确实没有找到与任务相关的入口时,才允许使用搜索框。任务中出现的"排行榜""榜单""热门""歌单""分类"永远是导航目标,不是搜索关键词。

## 筛选/排序
- 任务点名了排序或筛选条件(如少换乘/低价优先/评分最高/播放最多/最新等)时:
  优先在筛选/排序标签栏里找到对应标签并点击。
- 若当前这屏没看到该标签,先在标签栏所在的水平高度横向滑动(左/右)去找,
  两个方向都确认没有后再考虑替代方案,不要因为没看到就断定它不存在。

## Verify Each Step(防止误判完成)
最常见的失误,是"看到一个看起来像成功的提示就以为任务完成了"。请按下面纪律执行:

- 任务里的"关键词"——具名容器(歌单/书单/清单/收藏夹/购物车/特定文件夹/标签)、
  具体动作(播放/导航/发布/发送/排序/筛选/预订/支付到确认页)、数量要求(N 个)、
  筛选条件(评分>X / 价格<Y / 最新 / 最少换乘 / 0-10分钟)——
  每一个关键词都必须在**当前截图**里找到**逐字对应**的可见证据,
  才能判定相关子目标完成。

- 当 UI 反馈"类似但不一致"时,**判定为未完成**,继续动作。常见陷阱:
    · 任务要求"加入<具名容器>"  截图只显示"已收藏 / 已喜欢 / 已加心"      → 未完成
    · 任务要求"播放 X"          截图只显示 X 的搜索结果列表                → 未完成
    · 任务要求"发布评论"        截图只显示输入框已填好评论但未点发送        → 未完成
    · 任务要求"开始导航"        截图只显示路线规划页                       → 未完成
    · 任务要求"按播放量排序"    排序面板已展开但还没点确认                  → 未完成
    · 任务要求"预订房型 Z"      只加入了购物车 / 只看到房型详情             → 未完成
    · 任务要求"加入指定文件夹"  只显示"已收藏"或加到了默认文件夹            → 未完成

- "已收藏 / 已喜欢 / 已加心" 是 **中间状态**,不是 **任务完成**。除非任务字面只写
  "收藏",否则看到这两个词不要立即推进流程或 complete。

## Two-Step Pattern(主按钮 → 选择/确认面板 → 底部按钮)
许多 App 的"加入/分享/添加/发送给/标记到"是两层结构,**先识别再完整执行**:

L1 — 在内容详情页/列表项上点一个快捷按钮(♡ / + / 分享 / 添加 / 加入 / 标记 / …)
       → 弹出一个底部面板或全屏覆盖层。
       ⚠️ 这一步只是"打开了选择/确认面板",动作还没真的执行到位。

二级面板的通用可见特征(出现以下任意 2 项基本可断定是 L2 面板):
  (1) 顶部带"已收藏/已加心/已放入..."等中间状态提示语
  (2) 中间一个列表,展示已有的容器/分组/选项,每项右侧带圆形或方形选择框
  (3) 一个"+新建XX / 新建 / 创建"入口
  (4) 底部一个醒目按钮:"确定 / 完成 / 加入 / 添加到 / 分享 / 发送"

L2 — 必须在这个面板里完成下面任一路径:
       (a) 目标已在列表里  → 勾选目标项右侧选择框 → 点底部醒目按钮
       (b) 目标不在列表里  → 点"+新建XX" → 输入容器名 → 完成/确定
                          → 勾选刚建好的容器 → 点底部醒目按钮

L2 完成证据:底部按钮被点击后,面板自动关闭 / 出现"已加入<容器名>"或
"已发送 / 已添加"等明确提示 —— 这一次动作才算执行到位。

纪律:
- 面板上的 X / 取消 / 暂不 / 跳过 / 关闭 = **放弃 L2**,只完成 L1 的半成品。
  L2 没完成前**绝不**点这些。
- 已新建过的容器,后续 item 都加入同一个,**不要每次都新建**。
  请用 `Note: 已建容器=XX` 记录,后续在面板里直接勾选 XX。
- 二级面板里如果出现了多个选项(如多个候选歌单/收藏夹),要明确选中**任务要求的那个**;
  没有指定就选第一个或新建一个简短合理名。

## Counting Tasks(N 个/N 首/N 条)
- 任务里出现"N 个 / N 首 / N 条 / 前 N"等数量时,把任务理解为"把单 item 完整流程
  (含 L1+L2)跑 N 次",不是"找出 N 个候选就完事"。
- Progress 里必须强制写"K/N done",其中每个"done"都对应一次能在 Recent Steps
  或当前截图里找到的 L2 完成证据(底部按钮点击 / "已加入/已发送" 提示 / 项目出现
  在容器内容里)。
- K < N 时绝不 complete;K == N 时再做一遍最终自检后才能 complete。

## Side-effect Policy(何时停手 —— 仅一种情况:真实付款)
- 指令明确要求的动作就是任务目标,必须真正点击完成,包括:搜索、播放、筛选、排序、
  发布/发送评论、点赞收藏、加入书单/歌单/清单、新建容器、开始导航等。完成后再
  complete(content='done')。
- 仅在"会真实扣款/下单付款"的最后一步之前停手:立即支付 / 确认支付 / 确认付款 /
  立即付款 / 提交订单(并付款) / 确认下单 / 立即购买 / 确认购买 / 去结算并付款 等。
  对这类付费任务,把流程推进到"订单确认页 / 支付确认页"即视为完成,不要点最终付款
  按钮,直接 complete。
- 除"真实付款"外,不要提前 complete。
"""


class Agent(BaseAgent):
    """单模型 GUI Agent:紧凑记忆 + Notetaker + reflection + 反重复兜底。"""

    MAX_RECENT_VISUAL_TURNS = 2
    MAX_RECENT_TEXT_STEPS = 8
    MAX_COMPRESSED_LINES = 12
    MAX_THOUGHT_CHARS = 200      # 略增以容纳 LastEffect + Progress + Next
    MAX_RAW_OUTPUT_CHARS = 600
    MAX_TASK_NOTES = 24

    MODEL_IMAGE_LONG_SIDE = 1400
    MODEL_IMAGE_MIN_SHORT_SIDE = 980
    MODEL_IMAGE_MAX_SCALE = 2.0
    PREV_IMAGE_LONG_SIDE = 900
    JPEG_QUALITY = 88

    ENABLE_TARGET_BOX_REPAIR = True

    REPEAT_WARN_THRESHOLD = 2
    REPEAT_FORCE_THRESHOLD = 3

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
        _pre_tap_state["point"] = [500, 122]   # 任务切换时重置坐标
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
            "竖直:顶栏 y<150,内容区 y≈150~850,底部导航/输入区 y>850。"
        )

    def _build_memory_text(self, input_data: AgentInput) -> str:
        lines = ["## Compact Memory", f"Current step: {input_data.step_count}"]

        if self._task_notes:
            lines.append("\n### Task Notes(已记录事实,按原样使用,勿改写)")
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
                "请只输出一行合法的 Action(含 point/参数)。"
            )

        # —— Decision Reminder:全部 App-agnostic ——
        lines.append(
            "\n### Decision Reminder\n"
            "- LastEffect 只能基于当前截图客观陈述,不能用'应该/已经/想必'这类推断词。\n"
            "- 中间状态(已收藏 / 已喜欢 / 加入购物车 / 路线规划页 / 排序面板展开) ≠ 任务完成。\n"
            "- 看到二级面板(列表 + 选择框 + 底部'确定/加入/完成'按钮)时,任务还没执行到位,\n"
            "  必须先在面板里完成勾选并点底部按钮;不要点 X / 取消 / 暂不 关闭面板。\n"
            "- 计数任务(N 个)Progress 必须写 K/N done,K < N 不能 complete。\n"
            "- 已新建过的容器,后续 item 复用同一个,用 Note 记一笔,不要每次都新建。"
        )
        return "\n".join(lines)

    def _count_click_type_loops(self) -> int:
        """统计 recent_text_steps 末尾连续的 CLICK→TYPE 交替对数。"""
        actions = [s.get("action") for s in self._recent_text_steps]
        count = 0
        i = len(actions) - 1
        while i >= 1:
            if actions[i] == ACTION_TYPE and actions[i - 1] == ACTION_CLICK:
                count += 1
                i -= 2
            else:
                break
        return count

    def _build_repeat_warning(self) -> str:
        # CLICK→TYPE 交替循环:输入一直未生效的信号
        loop_count = self._count_click_type_loops()
        if loop_count >= 2:
            return (
                "\n### CLICK→TYPE 循环警告\n"
                f"- 已连续 {loop_count} 次 CLICK(搜索框)→ TYPE,但输入未生效。\n"
                "- 若仍在搜索页:先 wait(seconds='1') 再重试;"
                "若已退出搜索页:先导航回搜索框页面再重试。\n"
                "- 不要无限循环;三次不生效请改用 scroll 或 back。"
            )
        if self._consecutive_repeat >= self.REPEAT_FORCE_THRESHOLD:
            return (
                "\n### Stuck Warning\n"
                f"- 你已连续 {self._consecutive_repeat} 次重复同一动作且界面无效果。"
                "必须换一种策略:换点击目标 / 滚动寻找 / back 返回上一层。"
            )
        if self._consecutive_repeat >= self.REPEAT_WARN_THRESHOLD:
            return (
                "\n### Repeat Warning\n"
                f"- 你已连续 {self._consecutive_repeat} 次重复同一动作。"
                "请确认它是否真的生效;若界面没变化,请改用其它动作。"
            )
        return ""

    def generate_messages(self, input_data: AgentInput) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt(input_data.instruction)},
            {"role": "user", "content": self._build_memory_text(input_data)},
        ]

        for turn in self._recent_visual_turns[-self.MAX_RECENT_VISUAL_TURNS:]:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"上一步(Step {turn.get('step')})执行前的截图,用于判断上一步是否生效:"},
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
                    {"type": "text", "text": "当前截图。请基于这一屏决定唯一的下一步动作:"},
                    {"type": "image_url", "image_url": {"url": self._encode_model_image(input_data.current_image)}},
                ],
            }
        )
        return messages

    # ---------- 图像编码 ----------

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
                      dx, dy = en[0] - sn[0], en[1] - sn[1]
                      dist = (dx * dx + dy * dy) ** 0.5
                      if dist > 250:
                          r = 250 / dist
                          en = [int(round(sn[0] + dx * r)), int(round(sn[1] + dy * r))]
                      normalized["start_point"] = sn
                      normalized["end_point"] = en
        elif action == ACTION_TYPE:
            text = str(params.get("text", ""))
            # 纯换行保留(触发 Enter 提交);其余只去尾随换行
            if text and text.strip("\r\n") == "":
                normalized["text"] = "\n"
            else:
                normalized["text"] = text.rstrip("\r\n")
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

        # 更新 pre-tap 坐标:顶部区域 CLICK → 记住坐标供下次 TYPE 前置使用
        if parsed.get("action") == ACTION_CLICK:
            pt = (parsed.get("parameters") or {}).get("point")
            if isinstance(pt, list) and len(pt) == 2:
                _, y_pt = pt[0], pt[1]
                if isinstance(y_pt, (int, float)) and y_pt < _PRE_TAP_TOP_Y_MAX:
                    _pre_tap_state["point"] = [int(pt[0]), int(pt[1])]
                    logger.info(f"Pre-tap updated to {_pre_tap_state['point']} (top CLICK y={y_pt:.0f})")

    # ---------- LLM ----------

    def _call_llm(self, messages: List[Dict[str, Any]]) -> Tuple[str, Optional[UsageInfo]]:
        try:
            response = self._call_api(messages=messages, temperature=0.1, top_p=0.95)
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

        self._add_notes(self._extract_notes(raw_output))

        parsed = self._normalize_parsed_action(parse_model_output(raw_output))
        if not self._is_valid_parsed_action(parsed):
            self._parse_fail_count += 1
            logger.warning("解析失败,尝试兜底恢复")
            parsed = self._normalize_parsed_action(self._recover_action_from_raw(raw_output))

        if not self._is_valid_parsed_action(parsed):
            logger.error("无法恢复合法动作,改为 WAIT")
            parsed = {"action": ACTION_WAIT, "parameters": {"seconds": 2}, "raw_action": "fallback_wait"}

        parsed = self._repair_click_coordinates(raw_output, parsed)
        parsed = self._normalize_parsed_action(parsed)
        if not self._is_valid_parsed_action(parsed):
            parsed = {"action": ACTION_WAIT, "parameters": {"seconds": 2}, "raw_action": "fallback_wait_after_repair"}

        self._update_repeat_state(parsed)
        if (
            self._consecutive_repeat >= self.REPEAT_FORCE_THRESHOLD
            and parsed.get("action") not in (ACTION_COMPLETE, ACTION_WAIT)
        ):
            logger.warning(f"连续重复 {self._consecutive_repeat} 次,强制恢复动作")
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