"""Small tutor practice set plus the full graded high-school vocabulary.

Vocabulary source and licence: THIRD_PARTY_LICENSES/word-forest-LICENSE.txt.
"""
from __future__ import annotations

import json
from pathlib import Path

from .scenes import SCENES

GRADES = ["高一", "高二", "高三"]

# Conversation scenes. ``knowledge`` is what the AI is told to make the
# student practise, so a scene is never just a topic — it carries the grammar
# and functional language the student's grade is working on.
TOPICS = [
    {
        "id": "campus",
        "title": "校园生活",
        "english_title": "Campus life",
        "description": "聊课程、社团和一天的安排",
        "level": "高一",
        "knowledge": ["一般过去时叙述经历", "because / so 说明原因", "课程与社团词汇"],
        "roles": ["同班同学", "社团学长", "新转来的同桌", "值日老师", "食堂阿姨"],
        "openings": [
            "Hey, you look lost. Which class are you heading to?",
            "Sorry to stop you — do you know where the science lab is?",
            "You were in that group project too, right? How is your part going?",
        ],
        "opening": "Hi! Tell me about one class or activity you enjoyed at school this week.",
        "prompts": [
            "What made that class or activity interesting?",
            "How did it help you learn something new?",
            "Would you recommend it to a classmate? Why?",
        ],
    },
    {
        "id": "cafe",
        "title": "咖啡店点单",
        "english_title": "Ordering at a cafe",
        "description": "点单、改单、确认堂食还是外带",
        "level": "高一",
        "knowledge": ["I'd like / Could I have 点单句", "可数不可数名词与量词", "for here or to go 确认信息"],
        "roles": ["咖啡店店员", "排在你后面的顾客", "值班店长", "兼职学生店员", "外卖取餐员"],
        "openings": [
            "Hi there — what can I get started for you today?",
            "Sorry for the wait, we are short of staff. What would you like?",
            "That's the last croissant. Do you want it before someone else does?",
        ],
        "opening": "Hi! What would you like to drink today?",
        "prompts": [
            "Would you like that hot or iced?",
            "Is that for here or to go?",
            "Anything else with that?",
        ],
    },
    {
        "id": "directions",
        "title": "问路指路",
        "english_title": "Asking the way",
        "description": "在陌生街区问路，听懂并复述路线",
        "level": "高一",
        "knowledge": ["方位介词 next to / across from / behind", "祈使句指路 Go straight / Turn left", "How far / How long 提问"],
        "roles": ["路口的交警", "便利店店员", "晨跑的邻居", "公交站等车的老人", "校园保安"],
        "openings": [
            "You have been looking at your phone for a while — are you lost?",
            "The library? Sure, but the main entrance is closed today. Where are you trying to go?",
            "I am walking that way anyway. Which building do you need?",
        ],
        "opening": "You look lost. Where are you trying to go?",
        "prompts": [
            "Do you want the fastest way or the easiest one?",
            "How far do you think it is from here?",
            "What landmark is near the place you need?",
        ],
    },
    {
        "id": "doctor",
        "title": "看医生",
        "english_title": "At the doctor's",
        "description": "描述症状、说明持续时间、听懂医嘱",
        "level": "高一—高二",
        "knowledge": ["have a headache / It hurts when... 描述症状", "should / had better 给建议", "持续时间与频率表达"],
        "roles": ["校医", "社区诊所医生", "急诊分诊护士", "药店药师", "陪你来的同学"],
        "openings": [
            "Come in and sit down. What seems to be the problem?",
            "You look pale. How long have you been feeling like this?",
            "Before I check you, tell me where it hurts most.",
        ],
        "opening": "Come in and sit down. What seems to be the problem?",
        "prompts": [
            "How long have you had this?",
            "Does it hurt more in the morning or at night?",
            "Have you taken anything for it?",
        ],
    },
    {
        "id": "interview",
        "title": "社团面试",
        "english_title": "Club interview",
        "description": "自我介绍、讲经历、回答为什么想加入",
        "level": "高二",
        "knowledge": ["现在完成时谈经历", "定语从句 who / which 修饰", "自我介绍的功能句"],
        "roles": ["学生会主席", "辩论社社长", "志愿者协会负责人", "校刊主编", "篮球队队长"],
        "openings": [
            "Thanks for coming. Give me thirty seconds — who are you and why this club?",
            "Your form says you have done something similar before. Tell me about it.",
            "We only have two places left. What would you bring to the team?",
        ],
        "opening": "Thanks for coming. Tell me a little about yourself.",
        "prompts": [
            "What have you done that prepared you for this?",
            "What would you do if a teammate stopped showing up?",
            "How much time can you give us each week?",
        ],
    },
    {
        "id": "environment",
        "title": "绿色行动",
        "english_title": "A greener school",
        "description": "用英语提出一个校园环保方案",
        "level": "高二",
        "knowledge": ["被动语态 is used / can be recycled", "if 条件句提出方案", "提建议 Why not / We could"],
        "roles": ["环保社社长", "班里的卫生委员", "社区志愿者", "后勤老师", "隔壁班同学"],
        "openings": [
            "Our club meets in ten minutes. Can you help me move these boxes first?",
            "Someone left the lights on in the lab again. What should we do about it?",
            "I want to start one small green habit this week. Any idea?",
        ],
        "opening": "Our school wants to become greener. What is one change you would suggest?",
        "prompts": [
            "Who would take part in this change?",
            "What difficulty might you face, and how would you solve it?",
            "How would you know whether the plan works?",
        ],
    },
    {
        "id": "travel",
        "title": "机场与酒店",
        "english_title": "Airport and hotel",
        "description": "办登机、托运超重行李、入住改房",
        "level": "高二",
        "knowledge": ["Could you... / I'd like to... 礼貌请求", "现在完成时 have booked", "数字、时间与日期表达"],
        "roles": ["值机柜台职员", "酒店前台", "安检人员", "出租车司机", "同航班的旅客"],
        "openings": [
            "Good morning. May I see your passport and booking, please?",
            "Your bag is three kilos over. Do you want to pay the fee or move something to your carry-on?",
            "I have your reservation, but the room type you asked for is gone. What would you prefer?",
        ],
        "opening": "Good morning. May I see your passport and booking, please?",
        "prompts": [
            "Do you have any bags to check in?",
            "Would you prefer a window or an aisle seat?",
            "How many nights are you staying with us?",
        ],
    },
    {
        "id": "future",
        "title": "未来规划",
        "english_title": "Plans after school",
        "description": "练习表达目标、原因和具体行动",
        "level": "高三",
        "knowledge": ["不定式作目的状语", "名词性从句 what I want to do", "因果论证 because / therefore"],
        "roles": ["升学顾问", "实习带你的前辈", "大学招生官", "家里的大人", "已经上大学的朋友"],
        "openings": [
            "You said you were thinking about your next step. What is on your mind?",
            "I have five minutes before my next meeting — tell me your plan.",
            "If you had to choose one goal for this year, what would it be?",
        ],
        "opening": "What is one goal you hope to achieve in the next two years?",
        "prompts": [
            "Why does this goal matter to you?",
            "What is the first small step you can take?",
            "What support might you need from other people?",
        ],
    },
    {
        "id": "debate",
        "title": "观点讨论",
        "english_title": "Taking a side",
        "description": "就校园手机管理表达立场并回应反驳",
        "level": "高三",
        "knowledge": ["让步与转折 On one hand / However", "非谓语作主语 Doing... helps...", "虚拟语气 If I were..."],
        "roles": ["辩论对手", "班主任", "学生代表", "家长委员会成员", "校报记者"],
        "openings": [
            "Our school may ban phones during breaks. Where do you stand on that?",
            "You argued for the change last time. What made you so sure?",
            "Most parents here disagree with you. How would you answer them?",
        ],
        "opening": "Our school is discussing a phone ban. What is your view?",
        "prompts": [
            "What is the strongest argument against your view?",
            "How would you answer someone who disagrees with you?",
            "What evidence would change your mind?",
        ],
    },
]

WORDS = [
    {
        "id": "w01", "word": "achieve", "phonetic": "/əˈtʃiːv/", "part_of_speech": "v.",
        "meaning": "实现；达到", "topic": "future",
        "example": "You can achieve your goal with a clear plan.",
        "example_zh": "有一个清晰的计划，你就能实现目标。",
    },
    {
        "id": "w02", "word": "challenge", "phonetic": "/ˈtʃælɪndʒ/", "part_of_speech": "n.",
        "meaning": "挑战；难题", "topic": "campus",
        "example": "The science project was a real challenge for our team.",
        "example_zh": "这个科学项目对我们团队来说是个真正的挑战。",
    },
    {
        "id": "w03", "word": "confident", "phonetic": "/ˈkɒnfɪdənt/", "part_of_speech": "adj.",
        "meaning": "自信的；有把握的", "topic": "campus",
        "example": "Practice makes me feel more confident when I speak English.",
        "example_zh": "练习让我在说英语时更自信。",
    },
    {
        "id": "w04", "word": "responsibility", "phonetic": "/rɪˌspɒnsəˈbɪləti/", "part_of_speech": "n.",
        "meaning": "责任；职责", "topic": "future",
        "example": "Being a group leader taught me responsibility.",
        "example_zh": "担任小组长教会了我承担责任。",
    },
    {
        "id": "w05", "word": "reduce", "phonetic": "/rɪˈdjuːs/", "part_of_speech": "v.",
        "meaning": "减少；降低", "topic": "environment",
        "example": "We can reduce waste by using fewer paper cups.",
        "example_zh": "我们可以通过少用纸杯来减少浪费。",
    },
    {
        "id": "w06", "word": "schedule", "phonetic": "/ˈʃedjuːl/", "part_of_speech": "n.",
        "meaning": "日程安排；时间表", "topic": "campus",
        "example": "My schedule leaves enough time for reading after dinner.",
        "example_zh": "我的日程安排为晚餐后的阅读留出了足够的时间。",
    },
    {
        "id": "w07", "word": "volunteer", "phonetic": "/ˌvɒlənˈtɪə/", "part_of_speech": "n. / v.",
        "meaning": "志愿者；自愿做", "topic": "campus",
        "example": "I volunteer at the school library every Friday.",
        "example_zh": "我每周五在学校图书馆做志愿服务。",
    },
    {
        "id": "w08", "word": "suggestion", "phonetic": "/səˈdʒestʃən/", "part_of_speech": "n.",
        "meaning": "建议；提议", "topic": "environment",
        "example": "Her suggestion helped us improve the recycling plan.",
        "example_zh": "她的建议帮助我们改进了回收计划。",
    },
    {
        "id": "w09", "word": "improve", "phonetic": "/ɪmˈpruːv/", "part_of_speech": "v.",
        "meaning": "改善；提高", "topic": "future",
        "example": "Recording yourself can improve your pronunciation.",
        "example_zh": "录下自己的声音可以改善你的发音。",
    },
    {
        "id": "w10", "word": "opportunity", "phonetic": "/ˌɒpəˈtjuːnəti/", "part_of_speech": "n.",
        "meaning": "机会；时机", "topic": "future",
        "example": "The English club gives me an opportunity to practise speaking.",
        "example_zh": "英语俱乐部给了我练习口语的机会。",
    },
    {
        "id": "w11", "word": "convenient", "phonetic": "/kənˈviːniənt/", "part_of_speech": "adj.",
        "meaning": "方便的；便利的", "topic": "campus",
        "example": "It is convenient to review new words on my way home.",
        "example_zh": "在回家的路上复习新单词很方便。",
    },
    {
        "id": "w12", "word": "environment", "phonetic": "/ɪnˈvaɪrənmənt/", "part_of_speech": "n.",
        "meaning": "环境；周围状况", "topic": "environment",
        "example": "Small daily choices can protect the environment.",
        "example_zh": "日常的小选择可以保护环境。",
    },
]

SENTENCES = [
    {
        "id": "s01", "word_id": "w03", "topic": "campus",
        "text": "Practice makes me feel more confident when I speak English.",
        "translation": "练习让我在说英语时更自信。", "focus": "注意 confident 的重音在第一个音节。",
    },
    {
        "id": "s02", "word_id": "w05", "topic": "environment",
        "text": "We can reduce waste by using fewer paper cups.",
        "translation": "我们可以通过少用纸杯来减少浪费。", "focus": "reduce 的第二个音节要读清楚。",
    },
    {
        "id": "s03", "word_id": "w06", "topic": "campus",
        "text": "My schedule leaves enough time for reading after dinner.",
        "translation": "我的日程安排为晚餐后的阅读留出了足够的时间。", "focus": "在 enough time 前自然停顿。",
    },
    {
        "id": "s04", "word_id": "w01", "topic": "future",
        "text": "You can achieve your goal with a clear plan.",
        "translation": "有一个清晰的计划，你就能实现目标。", "focus": "achieve 的尾音 /v/ 不要省略。",
    },
    {
        "id": "s05", "word_id": "w10", "topic": "future",
        "text": "The English club gives me an opportunity to practise speaking.",
        "translation": "英语俱乐部给了我练习口语的机会。", "focus": "an opportunity 之间自然连读。",
    },
    {
        "id": "s06", "word_id": "w12", "topic": "environment",
        "text": "Small daily choices can protect the environment.",
        "translation": "日常的小选择可以保护环境。", "focus": "protect the 的辅音衔接要轻。",
    },
]


_VOCAB_FILE = Path(__file__).with_name("vocabulary.json")
_VOCAB_DATA = json.loads(_VOCAB_FILE.read_text(encoding="utf-8"))
VOCABULARY = _VOCAB_DATA["words"]
VOCAB_META = _VOCAB_DATA["meta"]
VOCAB_BY_ID = {item["id"]: item for item in VOCABULARY}
VOCAB_BY_WORD = {item["word"].lower(): item for item in VOCABULARY}
PRACTICE_COUNT_BY_GRADE = {
    grade: sum(1 for item in VOCABULARY if item["grade"] == grade and item.get("practice", True))
    for grade in GRADES
}


def vocabulary_for_grade(grade: str) -> list[dict]:
    return [
        item for item in VOCABULARY
        if item["grade"] == grade and item.get("practice", True)
    ]


def public_word(item: dict) -> dict:
    return {
        key: item[key]
        for key in ("id", "word", "phonetic", "part_of_speech", "meaning", "grade", "book", "unit")
        if key in item
    }


def scenes_for_grade(grade: str) -> list[dict]:
    resolved = []
    for scene in SCENES:
        if scene["grade"] != grade:
            continue
        words = []
        for raw_word in scene["words"]:
            item = VOCAB_BY_WORD.get(raw_word.lower())
            if item:
                words.append(public_word(item))
        resolved.append({
            **{key: value for key, value in scene.items() if key != "words"},
            "words": words,
        })
    return resolved


def topic_by_id(topic_id: str):
    return next((item for item in TOPICS if item["id"] == topic_id), None)


def word_by_id(word_id: str):
    return next((item for item in WORDS if item["id"] == word_id), None) or VOCAB_BY_ID.get(word_id)


def sentence_by_id(sentence_id: str):
    return next((item for item in SENTENCES if item["id"] == sentence_id), None)
