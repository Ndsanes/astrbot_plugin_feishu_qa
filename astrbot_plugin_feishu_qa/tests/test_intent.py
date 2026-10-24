"""意图闸门测试(2026-10-24 线上误答的回归)。

真实事故:群里问「脑袋,键盘和鼠标推荐哪些?」,机器人以 15.26 分主动答出
《连接 USB MIDI 键盘》。语料里没有任何选购建议,分数却落在真问题的分布
中段(5.18~22.32),阈值调不开——所以改由意图判定来决定"接不接"。

这里锁三件事,两个方向都要锁死:

1. 选购 / 推荐 / 议价 / 评价类提问必须判为 ``refused``(不接);名单覆盖常见
   写法,包括不含任何选购词的"这个声卡怎么样";
2. 真实故障与操作类提问**一条都不能被误拦**(误拦 = 把能答的问题吞掉);
3. 边界情形:先描述故障再顺口求建议("…怎么办,推荐怎么设置")要放行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.answer.intent import check_intent

FIXTURES = Path(__file__).parent / "fixtures"

# 「该拦」清单:线上原句 + 各种选购/议价/评价写法。
PURCHASE_QUESTIONS = [
    # 线上原句(梦杰 18:51)——就是这一条答错了
    "脑袋，键盘和鼠标推荐哪些？",
    "有什么好用的声卡推荐",
    "求推荐一个便宜的监听耳机",
    "推荐个话放呗",
    "这两款哪个好",
    "声卡和耳机哪个更适合编曲",
    "哪款更适合新手",
    "帮我挑一个声卡",
    "外置声卡多少钱",
    "声卡什么价位",
    "预算两千以内买什么声卡",
    "预算3000",
    "这个键盘性价比高吗",
    "值不值得入手这个音源",
    "这玩意值得吗",
    "想入手声卡",
    "midi 键盘什么牌子好",
    "哪个牌子的声卡适合新手",
    "我在哪买声卡",
    "买个声卡多少钱",
    "有没有便宜点的套装",
    "求购一个二手声卡",
    "录音设备推荐哪些",  # 有"录音"但没有任何故障线索 → 是选购,不是排障
    "声卡报错，买个新的多少钱",  # 故障词也不能豁免硬信号
    # 没有选购词的挑设备说法(靠器材名词 + 评价/选择句式识别)
    "有没有人用过这个声卡",
    "这个声卡可以吗",
    "小白选什么声卡好",
    "声卡该怎么挑",
    "编曲用什么声卡",
    "Cakewalk 用哪个声卡合适",
    "想换声卡",
    "想升级声卡",
    # 交易类
    "打个折吗",
    "支持支付宝吗",
    "你们卖不卖",
    "监听耳机怎么样",
    "这个声卡怎么样",
    "这个和那个比怎么样",
    "小钢炮好用不",
    "有什么建议吗",
    "买哪个",
    "我该买什么声卡",
]

# 「必须放行」清单:20 条真实措辞回归集 + 线上真实求助 + 故障后顺口求建议。
MUST_PASS_QUESTIONS = [
    "没有外置声卡 插耳机没声音还有电流声",
    "声卡设置没问题但是cakewalk就是没声音",
    "录音全是电流声失真",
    "打开cakewalk提示缺少dll文件",
    "cakewalk打开就闪退 vc库都装了也没用",
    "吉他录音延迟很大怎么办",
    "播放时有个轨道偶尔串出其他轨道的音符",
    "cakewalk怎么登录激活",
    "网页显示绿色标志但还是未激活",
    "bandlab的工程怎么转到sonar继续做",
    "midi键盘接上没声音",
    "复制midi的时候clip叠加了怎么办",
    "native access打开报错grant permission",
    "ample的免费音源装好后扫不出来",
    # 线上真实求助(同一晚的群消息)
    "我声卡插了耳机不出声怎么办",
    "脑袋我的硬盘炸了怎么办",
    "脑袋我为什么不能把轨道发送给自己",
    "键盘连不上电脑",
    "没声音",
    # 语料里真有"对比"类条目,所以不能把对比当意图词
    "免费版和会员版有什么区别",
    "Sonar 和 Cakewalk by BandLab 有没有区别",
    # 先描述故障、再顺口求建议:语料答得了,不能被软信号拦掉
    "录音延迟很大怎么办，推荐怎么设置",
    "导出的时候有问题有什么建议",
    "midi 键盘推荐用哪个驱动",
    "声音很小推荐怎么调",
    "导出设置怎么样调",
    # 闲聊/噪声:不该被意图闸门管(由分数闸门处理),更不该被 refused 吞掉
    "今天是什么日子？",
    "今天天气怎么样",
    "好耶",
    "awa",
    # 器材词 + 操作线索 = 操作问题,不是选购
    "这样设置可以吗",
    "这个报错用什么解决",
    "轨道用什么颜色",
    # 升级的是软件不是设备:"升级"不再单独构成选购信号
    "想升级cakewalk怎么办",
]


@pytest.fixture(scope="module")
def regression_queries() -> list[str]:
    data = json.loads((FIXTURES / "retrieval_queries.json").read_text(encoding="utf-8"))
    return [item["query"] for item in data["queries"]]


class TestRefusesPurchaseIntent:
    @pytest.mark.parametrize("query", PURCHASE_QUESTIONS)
    def test_blocked(self, query: str) -> None:
        verdict = check_intent(query)
        assert verdict.blocked, query
        assert verdict.reason in {"purchase_advice", "selection_advice"}
        assert verdict.matched, "命中词要留档，改词表时不用再猜"

    def test_extra_payload_for_decision_log(self) -> None:
        extra = check_intent("键盘和鼠标推荐哪些").as_extra()
        assert extra["intent"] == "purchase_advice"
        assert "推荐" in extra["intent_hits"]

    def test_evaluation_without_purchase_word_is_still_blocked(self) -> None:
        # 一个选购词都没有,但问的是"这设备怎么样"——语料没有评测内容
        verdict = check_intent("这个声卡怎么样")
        assert verdict.blocked and verdict.reason == "selection_advice"

    def test_empty_query_passes(self) -> None:
        assert check_intent("").blocked is False


class TestDoesNotBlockRealQuestions:
    """闸门必须窄:误拦比漏答更糟(用户明明问的是语料里的东西)。"""

    @pytest.mark.parametrize("query", MUST_PASS_QUESTIONS)
    def test_passes(self, query: str) -> None:
        assert check_intent(query).blocked is False, query

    def test_regression_queries_all_pass(self, regression_queries) -> None:
        blocked = [q for q in regression_queries if check_intent(q).blocked]
        assert blocked == []

    def test_troubleshooting_rescues_soft_suggestion(self) -> None:
        # "推荐"是软信号:句子里有故障线索时不拦
        assert check_intent("录音延迟很大怎么办，推荐怎么设置").blocked is False
        # 同样的"推荐",没有故障线索就拦
        assert check_intent("录音设备推荐哪些").blocked is True

    def test_hard_signal_not_rescued_by_troubleshooting(self) -> None:
        # "声卡报错,买个新的多少钱"问的还是买东西——硬信号不被故障线索豁免
        assert check_intent("声卡报错，买个新的多少钱").blocked is True


class TestKnownGaps:
    """已知漏网:写在这里而不是假装没有,方便以后按真实流量补词表。"""

    def test_pronoun_only_evaluation_is_not_recognised(self) -> None:
        """「这个能用吗」只有代词、没有器材名词 → 认不出来,放行。

        收紧到"没有器材名词也拦"会把"这样填可以吗"这类正常提问一起吞掉,
        所以宁可留这个缺口:它后面还有分数闸门兜底(分数不够就不会答)。
        """
        assert check_intent("这个能用吗").blocked is False
