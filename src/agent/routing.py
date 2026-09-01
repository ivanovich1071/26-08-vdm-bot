"""Кто отвечает на эту реплику и на каком этапе разговор.

Отдельный узел появился после жалобы заказчика: «пропал режим диалога, бот просто
связывает карточки с корзиной». Одному агенту со склеенным промптом на четыре с
лишним тысячи токенов приходилось одновременно быть справочной, продавцом и
охраной, и он честно выбирал самое простое — показать товар.

Теперь ролей две, и выбирает между ними этот модуль.

**Сначала правила.** «Привет», «2.1.14», «покажи станки для мастерских», попытка
вытащить промпт — всё это разбирается регулярками (`core/intent.py`) за
микросекунды и не стоит ничего. Так уходит большинство ходов.

**Модель — только на неоднозначном.** Короткий промпт без инструментов, ответ
строго JSON, предел в полтораста токенов: обращение обходится примерно в одну
сотую рубля. Сломанный JSON, отказ провайдера, неизвестное значение поля — не
повод ронять ход: возвращаем ветку подбора и идём дальше.

Решение оседает в профиле разговора (`core/profile.py`), а не в памяти агента:
от него зависит, покажет ли бот карточки, и после перезапуска бот не должен
снова вываливать позиции человеку, который только что сказал «дорого».
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.client import LLMError
from core import intent

log = logging.getLogger(__name__)

CONSULT = "consult"
SELL = "sell"
GUARD = "guard"

BRANCHES = {CONSULT, SELL, GUARD}
STAGES = {"diagnosis", "presentation", "objection", "closing"}
OBJECTIONS = {"price", "norm", "trust", "logistics", "docs", "none"}

# Сколько последних реплик показываем маршрутизатору. Ему нужен контекст («ладно,
# показывайте» — согласие на что?), но не весь разговор: это его цена.
HISTORY_LIMIT = 6
MAX_TOKENS = 200

# Попытка сменить роль или вытащить инструкцию. Ловится правилом, а не моделью:
# просить модель решить, атакуют ли её, — сомнительная затея.
_INJECTION = re.compile(
    r"систем\w+\s+промпт|system\s+prompt|покажи\s+(?:свои\s+)?(?:инструкц|промпт|правил)|"
    r"игнорируй\s+(?:все\s+)?предыдущ|забудь\s+(?:все\s+)?(?:предыдущ|инструкц|указан)|"
    r"представь,?\s+что\s+ты|притворись|веди\s+себя\s+как|ты\s+теперь\b|"
    r"выведи\s+(?:свой|своё|весь)\s+(?:промпт|текст\s+инструкц)|jailbreak|DAN\b",
    re.IGNORECASE,
)


@dataclass
class Decision:
    """Что решено по текущей реплике."""

    branch: str = SELL
    stage: str = "diagnosis"
    objection: str = "none"
    objection_handled: bool = False
    ready_to_see: bool = False
    facts: dict[str, str] = field(default_factory=dict)
    # Назван номер пункта перечня. Такой запрос точен сам по себе: показывать по
    # нему можно, не выясняя учреждение и зону.
    precise: bool = False
    # Чем принято решение: правилом, моделью или запасным путём. Идёт в журнал —
    # по нему видно, сколько ходов обошлись без обращения к модели.
    source: str = "правило"

    @property
    def sells(self) -> bool:
        return self.branch == SELL


def by_rules(text: str, profile) -> Decision | None:  # noqa: ANN001 — core.profile.DialogProfile
    """Решение, которое видно без модели. `None` — значит, нужно спрашивать."""
    text = (text or "").strip()
    if not text:
        return Decision(branch=CONSULT, source="правило")

    if _INJECTION.search(text):
        return Decision(branch=GUARD, source="правило")

    kind = intent.classify(text)

    if kind in (intent.GREETING, intent.SMALL_TALK):
        # Приветствие — это приветствие, а не запрос на пятьдесят товаров.
        return Decision(branch=CONSULT, source="правило")

    if kind == intent.NORM_QUESTION:
        return Decision(branch=CONSULT, source="правило")

    if kind == intent.NORM_CODE:
        # Назван пункт перечня — человек знает, чего хочет. Показываем.
        return Decision(
            branch=SELL,
            stage="presentation",
            ready_to_see=True,
            precise=True,
            source="правило",
        )

    if kind == intent.PRODUCT:
        # Прямая просьба показать переключает на продавца даже посреди
        # консультации — это оговорено с заказчиком отдельно. Названное
        # оборудование («нужен мяч для группы») — такая же просьба: спрашивать у
        # модели, о товаре ли речь, когда в реплике стоит слово «мяч», незачем.
        return Decision(branch=SELL, stage="presentation", ready_to_see=True, source="правило")

    # Осталось самое интересное: возражения, сомнения, согласия, ответы на
    # вопросы. Вот ради этого маршрутизатор и обращается к модели.
    return None


class Router:
    """Маршрутизатор: правила плюс дешёвый вызов модели на остатке."""

    def __init__(self, llm, prompt: str | None = None) -> None:  # noqa: ANN001 — LLMRouter
        self.llm = llm
        self.prompt = prompt if prompt is not None else _load_prompt()

    def decide(self, session, text: str) -> Decision:  # noqa: ANN001 — core.dialog.Session
        decision = by_rules(text, session.profile)
        if decision is None:
            decision = self._ask(session, text) or Decision(source="запасной")
        self._apply(session, decision)
        return decision

    # --- Обращение к модели ---------------------------------------------------

    def _ask(self, session, text: str) -> Decision | None:  # noqa: ANN001
        if self.llm is None or not self.llm.available:
            return None
        messages = [{"role": "system", "content": self.prompt}, *_history(session, text)]
        for client in self.llm.ready():
            try:
                message = client.complete(messages, temperature=0.0, max_tokens=MAX_TOKENS)
            except LLMError as exc:
                self.llm.mark_down(client, exc)
                continue
            _account(session, client, message)
            parsed = parse(message.get("content") or "")
            if parsed is not None:
                return parsed
            log.warning("Маршрутизатор не разобрал ответ модели, идём веткой подбора.")
            return None
        return None

    # --- Запись решения в профиль ---------------------------------------------

    def _apply(self, session, decision: Decision) -> None:  # noqa: ANN001
        profile = session.profile
        for key, value in decision.facts.items():
            _remember_fact(profile, key, value)

        profile.stage = decision.stage
        if decision.objection != "none":
            if decision.objection != profile.objection:
                # Возражение новое — считаем его неснятым, что бы ни сказала
                # модель: снять то, что человек только что высказал, нельзя.
                profile.objection = decision.objection
                profile.objection_handled = False
            elif decision.objection_handled:
                profile.objection_handled = True
        elif decision.objection_handled:
            profile.objection_handled = True

        pending = profile.objection != "none" and not profile.objection_handled
        if decision.ready_to_see:
            profile.ready_to_see = True
            if decision.objection == "none":
                # «Ладно, показывайте» — это и есть снятое возражение. Без этой
                # строки прошлое «дорого» держало карточки закрытыми до конца
                # разговора, даже когда человек прямо просил их показать.
                profile.objection_handled = True
        elif pending:
            # Пока возражение висит, карточки не показываем, даже если человек
            # хотел их посмотреть ходом раньше.
            profile.ready_to_see = False


def parse(raw: str) -> Decision | None:
    """Решение из ответа модели. Мусор превращается в `None`, а не в исключение."""
    data = _json_object(raw)
    if data is None:
        return None

    branch = str(data.get("branch") or "").strip().lower()
    stage = str(data.get("stage") or "").strip().lower()
    objection = str(data.get("objection") or "none").strip().lower()
    facts = data.get("facts")

    return Decision(
        branch=branch if branch in BRANCHES else SELL,
        stage=stage if stage in STAGES else "diagnosis",
        objection=objection if objection in OBJECTIONS else "none",
        objection_handled=bool(data.get("objection_handled")),
        ready_to_see=bool(data.get("ready_to_see")),
        facts=_clean_facts(facts if isinstance(facts, dict) else {}),
        source="модель",
    )


# --- Мелочи -------------------------------------------------------------------

# Поля профиля, которые маршрутизатору позволено заполнять. Персональных данных
# среди них нет и быть не должно: профиль хранится на диске и целиком уходит в
# промпт финального агента.
_ALLOWED_FACTS = ("institution", "room", "age", "budget", "deadline")
_MAX_FACT_LENGTH = 60


def _clean_facts(raw: dict) -> dict[str, str]:
    facts = {}
    for key in _ALLOWED_FACTS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            facts[key] = value.strip()[:_MAX_FACT_LENGTH]
    return facts


def _remember_fact(profile, key: str, value: str) -> None:  # noqa: ANN001
    """Дописывает факт, не затирая уже разобранное регулярками.

    Тип учреждения фиксируется один раз — в разговоре он не меняется. Остальное
    уточняется: человек начал со «спортзала», потом перешёл на «музыкальный зал».
    """
    if key == "institution" and profile.institution:
        return
    if getattr(profile, key, None) != value:
        setattr(profile, key, value)


def _history(session, text: str) -> list[dict[str, str]]:  # noqa: ANN001
    """Переписка для маршрутизатора — обязательно маскированная.

    В сессию реплики попадают уже с метками вместо телефонов и почты
    (`Session.remember`), поэтому история берётся как есть. А вот текущую реплику
    приходится маскировать здесь: ядро зовёт маршрутизатор после записи в
    историю, но тесты и другие вызовы могут этого не делать, и немаскированный
    телефон ушёл бы наружу. Проверено — уходил.
    """
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in session.history[-HISTORY_LIMIT:]
    ]
    masked = session.masker.mask(text or "")
    if not history or history[-1]["role"] != "user" or history[-1]["content"] != masked:
        history.append({"role": "user", "content": masked})
    return history


def _json_object(raw: str) -> dict | None:
    """Первый объект JSON в тексте.

    Модель то и дело оборачивает ответ в ```json … ``` или предваряет фразой,
    хотя промпт этого не просит. Вырезаем от первой фигурной скобки до последней.
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _account(session, client, message: dict) -> None:  # noqa: ANN001
    """Расход маршрутизатора идёт в тот же счётчик хода, что и расход агента."""
    from agent.agent import account_usage

    account_usage(session, client, message)


def _load_prompt() -> str:
    path = Path(__file__).parent / "prompts" / "router.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


__all__ = ["CONSULT", "GUARD", "SELL", "Decision", "Router", "by_rules", "parse"]
