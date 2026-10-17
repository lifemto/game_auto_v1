"""Observed task states. A transition records evidence, never a click return code alone."""


class WorkflowState:
    def __init__(self, bot, name, transitions, initial="start"):
        self.bot = bot
        self.name = name
        self.transitions = {key: frozenset(value) for key, value in transitions.items()}
        self.state = initial
        self.bot.log(f"[STATE] {name}: {initial}")
        records = getattr(self.bot, "account_records", None)
        if records:
            records.record(name, initial, "任务开始")

    def move(self, target, evidence):
        if target not in self.transitions.get(self.state, ()):
            raise ValueError(f"{self.name}非法状态转移: {self.state} -> {target}")
        if not evidence or not str(evidence).strip():
            raise ValueError(f"{self.name}状态转移缺少画面或业务证据: {target}")
        old = self.state
        self.state = target
        self.bot.log(f"[STATE] {self.name}: {old} -> {target}; {evidence}")
        records = getattr(self.bot, "account_records", None)
        if records:
            records.record(self.name, target, evidence)


TASK_TRANSITIONS = {
    "start": {"observing", "unknown"},
    "observing": {"acting", "unknown"},
    "acting": {"verified", "waiting", "skipped", "unknown"},
    "verified": {"complete", "unknown"},
    "complete": set(),
    "waiting": set(),
    "skipped": set(),
    "unknown": set(),
}


BEACON_TRANSITIONS = {
    "start": {"enter", "unknown"},
    "enter": {"scan", "complete", "unknown"},
    "scan": {"scan", "claim", "queue", "dispatch", "waiting", "complete", "unknown"},
    "claim": {"scan", "unknown"},
    "queue": {"scan", "waiting", "unknown"},
    "dispatch": {"scan", "queue", "unknown"},
    "waiting": {"scan", "queue", "unknown"},
    "complete": set(),
    "unknown": set(),
}


LUBU_TRANSITIONS = {
    "start": {"observe", "complete", "unknown"},
    "observe": {"observe", "own", "join", "waiting", "complete", "unknown"},
    "own": {"join", "prepare", "observe", "unknown"},
    "join": {"prepare", "observe", "unknown"},
    "prepare": {"depart", "complete", "unknown"},
    "depart": {"verify", "unknown"},
    "verify": {"observe", "complete", "unknown"},
    "waiting": {"observe", "complete", "unknown"},
    "complete": set(),
    "unknown": set(),
}
