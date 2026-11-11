"""Мини-игры в чате: викторина и «Стоп-кадр». Здесь только правила и счёт одной игры, без Telegram.

Игру (название, условия, раунды) админ собирает в панели, она хранится в таблице content с видом «game».
Идущая игра живёт в памяти: если бота перезапустить посреди игры, её придётся запустить заново.
"""
from dataclasses import dataclass, field

TYPES = {"quiz": "✨ Что я знаю о Небе? ✨", "photo": "📸 СТОП-КАДР 📸"}
KIND = "game"
MAX_ROUNDS = 30
MAX_OPTIONS = 8
# подпись к фото в Telegram не длиннее 1024 знаков, поэтому тексты игры ограничены с запасом
LIMITS = {"title": 60, "about": 600, "prizes": 300, "question": 500, "option": 40}
NUMBERS = {"need": (1, 200), "places": (1, 50)}


def new_game(kind: str) -> dict:
    return {"type": kind, "title": TYPES[kind], "about": "", "photo": None, "prizes": "", "places": 3, "need": 3,
            "retries": True, "signup": kind == "quiz", "rounds": []}


def signup(game: dict) -> bool:
    """Нужна ли запись перед игрой. Без записи сразу публикуется первый раунд, и играть может любой."""
    return game.get("signup", game["type"] == "quiz")  # игры, собранные до появления настройки


def parse_options(raw: str) -> list:
    """Варианты ответа: каждый с новой строки, а если строка одна — через запятую."""
    parts = raw.split("\n") if "\n" in raw.strip() else raw.split(",")
    return [p.strip() for p in parts if p.strip()]


@dataclass
class Session:
    chat_id: int
    game_id: int
    game: dict
    host: int                 # кто запустил: ему приходят фото на проверку и сообщение о конце игры
    thread: int | None
    state: str = "lobby"      # lobby → round ⇄ pause → done
    players: dict = field(default_factory=dict)   # id -> имя
    scores: dict = field(default_factory=dict)    # id -> [очки, секунды, потраченные на верные ответы]
    round: int = -1
    opened: float = 0         # когда начался раунд
    right: dict = field(default_factory=dict)     # id -> за сколько секунд ответил верно
    out: set = field(default_factory=set)         # кто ошибся и выбыл из раунда
    waiting: set = field(default_factory=set)     # «Стоп-кадр»: от кого ждём фото в личке
    pending: dict = field(default_factory=dict)   # «Стоп-кадр»: id -> на какой секунде прислал фото (ждёт проверки)
    post_id: int | None = None
    post_photo: bool = False

    # --- лобби ---
    def join(self, uid: int, name: str) -> str:
        if self.state != "lobby":
            return "over"
        if uid in self.players:
            return "already"
        self.players[uid] = name
        return "joined"

    def leave(self, uid: int) -> str:
        if self.state != "lobby":
            return "over"
        return "left" if self.players.pop(uid, None) else "not_in"

    @property
    def signup(self) -> bool:
        return signup(self.game)

    def enter(self, uid: int, name: str):
        """Игра без записи: человек становится игроком, когда впервые отвечает."""
        if self.state == "round" and not self.signup and uid not in self.players:
            self.players[uid] = name
            self.scores[uid] = [0, 0.0]

    @property
    def full(self) -> bool:
        return len(self.players) >= self.game["need"]

    # --- раунд ---
    @property
    def question(self) -> dict:
        return self.game["rounds"][self.round]

    @property
    def has_next(self) -> bool:
        return self.round + 1 < len(self.game["rounds"])

    @property
    def answered(self) -> int:
        return len(self.right) + len(self.out)

    @property
    def complete(self) -> bool:
        """С записью — ответили все записавшиеся. Без записи сколько будет игроков, заранее неизвестно, поэтому
        раунд закрывается, когда верно ответило столько, сколько призовых мест."""
        if self.signup:
            return self.answered >= len(self.players)
        return len(self.right) >= self.game["places"]

    def open(self, now: float):
        """Начинает следующий раунд."""
        if self.round < 0:
            self.scores = {uid: [0, 0.0] for uid in self.players}
        self.round += 1
        self.state, self.opened = "round", now
        self.right, self.out, self.waiting, self.pending = {}, set(), set(), {}

    def _gate(self, uid: int, rnd: int):
        """Почему этот человек не может сейчас отвечать (None — может)."""
        if self.state != "round":
            return "over"
        if rnd != self.round:
            return "old"
        if uid not in self.players:
            return "stranger"
        if uid in self.out:
            return "out"
        if uid in self.right:
            return "done"
        return None

    def answer(self, uid: int, rnd: int, option: int, now: float) -> str:
        """Ответ в викторине: right, retry (можно ещё раз), wrong (выбыл) или причина отказа."""
        stop = self._gate(uid, rnd)
        if stop:
            return stop
        if option == self.question["correct"]:
            self.right[uid] = now - self.opened
            return "right"
        if self.game["retries"]:
            return "retry"
        self.out.add(uid)
        return "wrong"

    def want_photo(self, uid: int, rnd: int) -> str:
        """Игрок нажал «Отправить скриншот»: ask — ждём фото в личке."""
        stop = self._gate(uid, rnd)
        if stop:
            return stop
        if uid in self.pending:
            return "pending"
        self.waiting.add(uid)
        return "ask"

    def submit(self, uid: int, now: float) -> bool:
        """Игрок прислал фото. Время ответа — когда прислал, а не когда ведущий проверил."""
        if self.state != "round" or uid not in self.waiting:
            return False
        self.waiting.discard(uid)
        self.pending[uid] = now - self.opened
        return True

    def unsubmit(self, uid: int):
        """Фото не дошло до ведущего — ждём его снова."""
        if self.pending.pop(uid, None) is not None:
            self.waiting.add(uid)

    def judge(self, uid: int, rnd: int, ok: bool) -> str:
        """Ведущий проверил фото: right, retry, wrong или over, если раунд уже закрыт."""
        if self.state != "round" or rnd != self.round or uid not in self.pending:
            return "over"
        seconds = self.pending.pop(uid)
        if ok:
            self.right[uid] = seconds
            return "right"
        if self.game["retries"]:
            self.waiting.add(uid)
            return "retry"
        self.out.add(uid)
        return "wrong"

    def close(self, bonus: int):
        """Закрывает раунд и начисляет очки. Возвращает ([(id, секунды, очки)] по скорости, [id остальных])."""
        self.state = "pause"
        rows = []
        for place, (uid, seconds) in enumerate(sorted(self.right.items(), key=lambda x: x[1])):
            points = 1 + (bonus if place == 0 else 0)
            self.scores[uid][0] += points
            self.scores[uid][1] += seconds
            rows.append((uid, seconds, points))
        return rows, [uid for uid in self.players if uid not in self.right]

    def standings(self):
        """[(id, очки, секунды)]: больше очков — выше, при равенстве выше тот, кто отвечал быстрее."""
        return sorted(((uid, sc, sec) for uid, (sc, sec) in self.scores.items()), key=lambda x: (-x[1], x[2]))
