"""提问意图闸门:判断"这句话问的东西,我们根本不接"。

背景(2026-10-24 线上观测)。群里问「脑袋,键盘和鼠标推荐哪些?」,机器人以
15.26 分主动答出《连接 USB MIDI 键盘》——其中 12.86 分来自症状标签「键盘」
的子串命中(标签命中固定 8.0 分,一个双字泛名词单独就过 HIGH=6.5)。语料是
"Cakewalk 出问题怎么解决",不含任何选购建议、型号推荐或价格;这类问题只要
撞上一个名词就会过线。

更关键的是,**阈值救不了它**:同一份语料上 20 条真实措辞回归集的 top1 分数
是 5.18 ~ 22.32(中位 14.14),而这条误命中是 15.26——落在真问题分布的中段。
把"泛标签不再满分"这类折扣加上,也只降到 11.26,仍然过线。所以必须在打分
之外补一层"这类问题我们接不接"的判断。

做法:**这类问题我们不承接**,判定为 ``refused``——不给答案,并且拦住主
Agent(``stop_event``),只回一句范围说明。产品边界(2026-10-24 明确):推荐/
选购能力(Modu)还没接进来,这类问题没有可靠答案;让它去撞文档,或者交给大
模型即兴编一份,都是错答。零 LLM、零网络、纯确定性。

判定分两级、四类信号(顺序即优先级):

1. **硬选购信号**(``PURCHASE_HARD_TOKENS``)——价格/预算/买/哪个好/牌子/
   二手/性价比…。命中即拒绝,**故障词不能豁免**:"声卡报错,买个新的多少钱"
   问的还是买东西。
2. **软求建议信号**(``PURCHASE_SOFT_TOKENS``)——推荐/求推/好用吗/求建议。
   这类词常出现在"先描述故障再顺口求建议"的句子里,所以句子里有**故障线索**
   (``PROBLEM_MARKERS``)时放行:"录音延迟很大怎么办,推荐怎么设置"。
3. **选择/评价句式**(``SELECTION_INTENT_TOKENS``)——"这个声卡怎么样""哪款
   更适合新手"。没有选购词也可能问的是评价,所以单独一层。
4. **操作线索**(``OPERATION_MARKERS``)——只用于第 3 层的豁免:"导出设置
   怎么样调"是操作问题,不是评测。第 2 层只认故障词,不认操作词,否则
   "录音设备推荐哪些"会被"录音"二字洗白。

词表刻意窄:只收"语料必然没有"的意图。「对比」「区别」不收,因为语料里真有
《Sonar 免费版与会员版对比》。错判两个方向都有代价:漏拦会答错,误拦会把能答
的问题吞掉——两侧清单都在 ``tests/test_intent.py``,含已知缺口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 第 1 层「硬选购信号」:命中即不接,故障线索也不豁免。
# 刻意不匹配单个"选"字——"选择轨道""选中音符"都是功能词。
PURCHASE_HARD_TOKENS: tuple[str, ...] = (
    "哪个好",
    "哪款",
    "哪一款",
    "哪一家",
    "哪家好",
    "哪个牌子",
    "什么牌子",
    "牌子",
    "品牌",
    "多少钱",
    "价格",
    "价位",
    "预算",
    "性价比",
    "值不值",
    "值不值得",
    "值得买",
    "值得入",
    "值得吗",
    "入手",
    "贵不贵",
    "买哪",
    "该买",
    "要不要买",
    "买什么",
    "买个",
    "买一个",
    "买一个",
    "选哪",
    "选哪个",
    "怎么选",
    "如何选",
    "测评",
    "开箱",
    "二手",
    "团购",
    "打折",
    "优惠",
    "便宜",
    "买",
    "卖",
    "付款",
    "支付宝",
    "微信支付",
    "下单",
)

# 硬信号里的句式(中间允许插字):"打个折""便宜一点"。
PURCHASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"打.{0,2}折"),
    re.compile(r"便宜.{0,2}点"),
    re.compile(r"能.{0,2}不.{0,2}能.{0,2}便"),
)

# 第 1 层「软求建议信号」:句子里有故障线索时放行(见 PROBLEM_MARKERS)。
PURCHASE_SOFT_TOKENS: tuple[str, ...] = (
    "推荐",
    "求推",
    "推一个",
    "推个",
    "好用吗",
    "好用不",
    "有什么建议",
    "有建议吗",
    "求建议",
)

# 第 3 层乙组(需器材名词):"这个声卡怎么样""用什么声卡""想换声卡"。这些词
# 单看太泛(「怎么样」「用什么」到处都是),只有配上器材名词才等于"在挑设备"。
GEAR_EVAL_TOKENS: tuple[str, ...] = (
    "怎么样",
    "好不好",
    "好用不",
    "能用吗",
    "可以吗",
    "行不行",
    "值得吗",
    "有必要吗",
    "用过",
)

# 真·选购句式:同样需要器材名词,但**操作线索也豁免不了**——"想升级声卡"里
# 那个"升级"是买方动作,不是 Cakewalk 的升级流程。
GEAR_PURCHASE_TOKENS: tuple[str, ...] = (
    "用什么",
    "用哪个",
    "选什么",
    "怎么挑",
    "想换",
    "想升级",
    "想买",
)

# 第 3 层甲组:选择句式。句子本身就在挑东西,不需要器材名词。
SELECTION_INTENT_TOKENS: tuple[str, ...] = (
    "哪个更适合",
    "哪个更",
    "哪款更",
    "挑一个",
    "挑个",
    "选一个",
    "选个",
    "比怎么样",
    # 「比较 / 对比」刻意不收:语料里真有《免费版与会员版对比》,那是能答的。
)

# 第 3 层乙组:评价句式("怎么样 / 好不好").这类词在中文里到处都是,所以必须
# 同时出现**器材名词**才算"在问这个设备怎么样"——"今天天气怎么样"不归我们管,
# 更不该被 refused 拦掉(那会把闲聊也一起吞了)。
GEAR_NOUNS: tuple[str, ...] = (
    "声卡",
    "耳机",
    "监听",
    "键盘",
    "鼠标",
    "麦克风",
    "话筒",
    "音箱",
    "音响",
    "显示器",
    "电脑",
    "主机",
    "显卡",
    "内存",
    "硬盘",
    "cpu",
    "音源",
    "插件",
    "midi",
    "话放",
    "前级",
    "声学",
    "功放",
    "摄像头",
    "设备",
    "套装",
)

# 故障线索:证明"这句话在描述出问题",不是在做购买决策。
PROBLEM_MARKERS: tuple[str, ...] = (
    "怎么办",
    "怎么解决",
    "出问题",
    "有问题",
    "报错",
    "错误",
    "失败",
    "无法",
    "不能",
    "不了",
    "不上",
    "不出",
    "不响",
    "没声",
    "没有声",
    "声音小",
    "声音很小",
    "很小",
    "音量小",
    "打不开",
    "闪退",
    "崩溃",
    "崩了",
    "卡顿",
    "卡住",
    "卡死",
    "很卡",
    "太卡",
    "延迟",
    "爆音",
    "杂音",
    "电流",
    "失真",
    "听不到",
    "看不到",
    "找不到",
    "搜不到",
    "扫不到",
    "丢失",
    "消失",
    "连不上",
    "识别",
    "驱动",
    "缺少",
    "冻结",
    "乱码",
    "变慢",
    "占用",
    "黑屏",
    "白屏",
    "重启",
)

# 操作线索:只给第 3 层做豁免("导出设置怎么样调")。不参与第 2 层,
# 否则"录音设备推荐哪些"里的"录音"会把一个选购问题洗白。
OPERATION_MARKERS: tuple[str, ...] = (
    "怎么用",
    "怎么连",
    "怎么装",
    "怎么调",
    "怎么设",
    "怎么改",
    "怎么弄",
    "怎么搞",
    "如何解决",
    "安装",
    "卸载",
    "激活",
    "注册",
    "登录",
    "导出",
    "导入",
    "设置",
    "配置",
    "连接",
    "录音",
    "播放",
    "同步",
    "重装",
    "更新",
    "升级",
    "加载",
    "扫描",
    "保存",
    "工程",
    "轨道",
    "界面",
    "按钮",
    "快捷键",
    "菜单",
    "窗口",
    "插件",
    "音源",
    "模板",
    "汉化",
    "中文",
)


@dataclass(frozen=True)
class IntentVerdict:
    """一次意图判定的结果。

    ``blocked`` 为真表示"这类问题我们目前不接";``matched`` 保留命中词,便于
    在决策日志里复盘是哪几个字触发了闸门(改词表时不必再猜)。
    """

    blocked: bool = False
    reason: str = ""
    matched: tuple[str, ...] = ()

    def as_extra(self) -> dict:
        if not self.blocked:
            return {}
        return {"intent": self.reason, "intent_hits": list(self.matched)}


def check_intent(query: str) -> IntentVerdict:
    """判定提问意图。纯函数,永不抛异常;大小写无关。

    空串直接放行(空问句由上游各自处理)。
    """
    text = (query or "").lower()
    if not text:
        return IntentVerdict()

    problem = any(m in text for m in PROBLEM_MARKERS)

    hard = tuple(t for t in PURCHASE_HARD_TOKENS if t in text)
    hard += tuple(m.group(0) for m in (p.search(text) for p in PURCHASE_PATTERNS) if m)
    if hard:
        return IntentVerdict(blocked=True, reason="purchase_advice", matched=hard)
    soft = tuple(t for t in PURCHASE_SOFT_TOKENS if t in text)
    if soft and not problem:
        return IntentVerdict(blocked=True, reason="purchase_advice", matched=soft)

    operation = any(m in text for m in OPERATION_MARKERS)
    selection = tuple(t for t in SELECTION_INTENT_TOKENS if t in text)
    if selection and not problem and not operation:
        return IntentVerdict(blocked=True, reason="selection_advice", matched=selection)

    gear = tuple(t for t in GEAR_NOUNS if t in text)
    if not gear or problem:
        return IntentVerdict()
    gear_purchase = tuple(t for t in GEAR_PURCHASE_TOKENS if t in text)
    if gear_purchase:
        return IntentVerdict(
            blocked=True, reason="purchase_advice", matched=gear_purchase + gear
        )
    gear_eval = tuple(t for t in GEAR_EVAL_TOKENS if t in text)
    if gear_eval and not operation:
        return IntentVerdict(
            blocked=True, reason="selection_advice", matched=gear_eval + gear
        )
    return IntentVerdict()
