"""评测查询集:Cakewalk Sonar Reference Guide(规格 §30-34)。

evidence 判定基准:相关小节的标题字符串(在 enhanced 的 breadcrumb/正文
与 baseline 的扁平文本中均出现,对三方公平)。
unanswerable 查询的主题确认不在手册中(手册是 Cakewalk Sonar DAW 参考)。
"""

from __future__ import annotations

import json
from pathlib import Path

QUERIES: list[dict] = [
    # ── exact(标题近原样提问)(§31)──
    {"query": "What is punch recording?", "category": "exact",
     "evidence": ["Punch recording"]},
    {"query": "What are MIDI Groove Clips?", "category": "exact",
     "evidence": ["MIDI Groove Clips"]},
    {"query": "What is the Step Sequencer view?", "category": "exact",
     "evidence": ["Step Sequencer view"]},
    {"query": "What is AudioSnap?", "category": "exact",
     "evidence": ["AudioSnap"]},
    {"query": "What is ProChannel?", "category": "exact",
     "evidence": ["ProChannel"]},
    {"query": "What is the Browser used for with loops?", "category": "exact",
     "evidence": ["Using loops with the Browser"]},

    # ── paraphrase(换说法)──
    {"query": "How do I record over a specific part of my track?", "category": "paraphrase",
     "evidence": ["Punch recording"]},
    {"query": "How can I save a marker at a certain position?", "category": "paraphrase",
     "evidence": ["Markers dialog", "Insert> Multiple Tracks"]},
    {"query": "Where do I manage my loops before adding them?", "category": "paraphrase",
     "evidence": ["Using loops with the Browser"]},
    {"query": "How does Sonar handle timing-stretched clips?", "category": "paraphrase",
     "evidence": ["How Groove Clips work in Sonar"]},
    {"query": "Can I fix the timing of a badly played recording?", "category": "paraphrase",
     "evidence": ["AudioSnap"]},
    {"query": "Is there a channel strip on every track?", "category": "paraphrase",
     "evidence": ["ProChannel"]},
    {"query": "How do I see notes on a staff?", "category": "paraphrase",
     "evidence": ["Notes pane", "Regenerate TAB"]},
    {"query": "How to revert a project to its saved state?", "category": "paraphrase",
     "evidence": ["File> Revert"]},

    # ── symptom(症状描述)──
    {"query": "My loop tempo changes when the project tempo is different, why?",
     "category": "symptom", "evidence": ["How Groove Clips work in Sonar", "MIDI Groove Clips"]},
    {"query": "Why can't I hear audio while punching in?",
     "category": "symptom", "evidence": ["Punch recording"]},
    {"query": "The sequencer grid doesn't show my drum sounds correctly",
     "category": "symptom", "evidence": ["Step Sequencer view", "Drum Grid"]},
    {"query": "Notes look wrong on the staff after I quantized them",
     "category": "symptom", "evidence": ["Staff view", "Regenerate TAB"]},
    {"query": "My time ruler shows weird numbers instead of bars",
     "category": "symptom", "evidence": ["Time Ruler Format> M:B:T"]},
    {"query": "Clock source seems off when syncing external gear",
     "category": "symptom", "evidence": ["Project - Clock (Advanced)", "Synchroni"]},

    # ── procedure(操作步骤)──
    {"query": "How to export audio to a WAV file?", "category": "procedure",
     "evidence": ["Exporting audio", "File> Export> Audio"]},
    {"query": "Steps to create markers from sections of a song", "category": "procedure",
     "evidence": ["Create Markers from Sections"]},
    {"query": "How do I import a Cakewalk Interchange file?", "category": "procedure",
     "evidence": ["File> Import> Cakewalk Interchange"]},
    {"query": "How to apply an effect to MIDI events?", "category": "procedure",
     "evidence": ["Process> Apply Effect> MIDI Effects"]},
    {"query": "How to print a preview of sheet music?", "category": "procedure",
     "evidence": ["Print Preview dialog"]},
    {"query": "How to select all articulations of a track's clips?", "category": "procedure",
     "evidence": ["Select Track Articulations with Clips"]},
    {"query": "How do I set the fade-out curve for crossfades?", "category": "procedure",
     "evidence": ["Default Fade-Out Curve"]},
    {"query": "How to hide several tracks at once?", "category": "procedure",
     "evidence": ["Hide Selected Tracks"]},

    # ── concept(概念理解)──
    {"query": "Explain the concept of track view in Sonar", "category": "concept",
     "evidence": ["Views> Track View"]},
    {"query": "What does meter peak option do on buses?", "category": "concept",
     "evidence": ["Bus Meter Options> Peak"]},
    {"query": "Understanding video files inside a project", "category": "concept",
     "evidence": ["Video view"]},
    {"query": "What is the arpeggiator for?", "category": "concept",
     "evidence": ["Using the arpeggiator"]},

    # ── cross-language(中文提问)──
    {"query": "什么是 Punch 录音?", "category": "cross-language",
     "evidence": ["Punch recording"]},
    {"query": "怎么导出音频文件?", "category": "cross-language",
     "evidence": ["Exporting audio", "File> Export> Audio"]},
    {"query": "MIDI 节拍切片是怎么工作的?", "category": "cross-language",
     "evidence": ["MIDI Groove Clips", "How Groove Clips work in Sonar"]},
    {"query": "如何创建标记?", "category": "cross-language",
     "evidence": ["Markers dialog", "Create Markers from Sections"]},
    {"query": "音频快照功能在哪里设置?", "category": "cross-language",
     "evidence": ["AudioSnap"]},
    {"query": "调音台通道条的说明", "category": "cross-language",
     "evidence": ["ProChannel"]},

    # ── distractor / unanswerable(手册中不存在的话题)──
    {"query": "How do I fix a blue screen on my Windows PC?", "category": "distractor",
     "answerable": False, "evidence": []},
    {"query": "What is the stock price of BandLab today?", "category": "distractor",
     "answerable": False, "evidence": []},
    {"query": "帮我写一首关于夏天的诗", "category": "distractor",
     "answerable": False, "evidence": []},
    {"query": "How to cook pasta carbonara?", "category": "distractor",
     "answerable": False, "evidence": []},
    {"query": "推荐一下最好的显卡用来打游戏", "category": "distractor",
     "answerable": False, "evidence": []},
]


def save(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(QUERIES, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "queries_cakewalk.json"
    save(out)
    cats: dict[str, int] = {}
    for q in QUERIES:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
    print(f"saved {len(QUERIES)} queries -> {out}")
    print("categories:", json.dumps(cats, ensure_ascii=False))
