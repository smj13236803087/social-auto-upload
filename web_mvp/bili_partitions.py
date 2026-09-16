"""Bilibili upload partition choices for the Web MVP UI.

Only leaf (二级) partitions are listed — B站投稿一般要选具体子分区。
Default matches the historical yt food sync script (`DEFAULT_BILI_TID = 249`).
"""

from __future__ import annotations

# (一级分区名, [(显示名, tid), ...])
BILIBILI_PARTITION_GROUPS: list[tuple[str, list[tuple[str, int]]]] = [
    (
        "美食",
        [
            ("美食制作", 76),
            ("美食侦探", 212),
            ("美食测评", 213),
            ("田园美食", 214),
            ("美食记录", 215),
        ],
    ),
    (
        "生活",
        [
            ("日常", 21),
            ("搞笑", 138),
            ("出行", 250),
            ("三农", 251),
            ("家居房产", 239),
            ("手工", 161),
            ("绘画", 162),
        ],
    ),
    (
        "运动",
        [
            ("篮球", 235),
            ("足球", 249),
            ("健身", 164),
            ("竞技体育", 236),
            ("运动文化", 237),
            ("运动综合", 238),
        ],
    ),
    (
        "音乐",
        [
            ("原创音乐", 28),
            ("翻唱", 31),
            ("演奏", 59),
            ("VOCALOID·UTAU", 30),
            ("现场演出", 29),
            ("MV", 193),
            ("乐评盘点", 243),
            ("音乐教学", 244),
            ("音乐综合", 130),
        ],
    ),
    (
        "舞蹈",
        [
            ("宅舞", 20),
            ("街舞", 198),
            ("明星舞蹈", 199),
            ("中国舞", 200),
            ("舞蹈综合", 154),
            ("舞蹈教程", 156),
        ],
    ),
    (
        "游戏",
        [
            ("单机游戏", 17),
            ("电子竞技", 171),
            ("手机游戏", 172),
            ("网络游戏", 65),
            ("桌游棋牌", 173),
            ("GMV", 121),
            ("音游", 136),
            ("Mugen", 19),
        ],
    ),
    (
        "知识",
        [
            ("科学科普", 201),
            ("社科·法律·心理", 124),
            ("人文历史", 228),
            ("财经商业", 207),
            ("校园学习", 208),
            ("职业职场", 209),
            ("设计·创意", 229),
            ("野生技能协会", 122),
        ],
    ),
    (
        "科技",
        [
            ("数码", 95),
            ("软件应用", 230),
            ("计算机技术", 231),
            ("科工机械", 232),
        ],
    ),
    (
        "动物圈",
        [
            ("喵星人", 218),
            ("汪星人", 219),
            ("大熊猫", 220),
            ("野生动物", 221),
            ("爬宠", 222),
            ("动物综合", 75),
        ],
    ),
    (
        "时尚",
        [
            ("美妆护肤", 157),
            ("仿妆cos", 252),
            ("穿搭", 158),
            ("时尚潮流", 159),
        ],
    ),
    (
        "汽车",
        [
            ("赛车", 245),
            ("改装玩车", 246),
            ("新能源车", 247),
            ("房车", 248),
            ("摩托车", 240),
            ("购车攻略", 227),
            ("汽车生活", 176),
        ],
    ),
    (
        "娱乐",
        [
            ("综艺", 71),
            ("娱乐杂谈", 241),
            ("粉丝创作", 242),
            ("明星综合", 137),
        ],
    ),
    (
        "影视",
        [
            ("影视杂谈", 182),
            ("影视剪辑", 183),
            ("短片", 85),
            ("预告·资讯", 184),
        ],
    ),
    ("VLOG", [("VLOG", 19)]),
]

# Match scripts/sync_yt_food.py DEFAULT_BILI_TID
DEFAULT_BILIBILI_TID = 249


def list_bilibili_partitions() -> list[dict]:
    groups = []
    for group_name, items in BILIBILI_PARTITION_GROUPS:
        groups.append(
            {
                "group": group_name,
                "items": [{"label": label, "tid": tid} for label, tid in items],
            }
        )
    return groups


def partition_label(tid: int) -> str:
    for group_name, items in BILIBILI_PARTITION_GROUPS:
        for label, value in items:
            if value == tid:
                return f"{group_name} · {label}"
    return f"分区 {tid}"
