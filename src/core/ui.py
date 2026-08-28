"""Примитивы ответа, общие для всех каналов.

Ядро никогда не собирает разметку Telegram или HTML виджета. Оно возвращает эти
объекты, а адаптер рендерит их по-своему: Telegram — карточкой с кнопками, виджет —
текстом и списком заказа. Благодаря этому логика продажи не размножается по каналам.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from catalog.models import Product

# Потолок раскладки берём по самому строгому каналу — MAX: до 7 кнопок в ряду,
# до 3, если это кнопки-ссылки. Иначе интерфейс разъедется между каналами.
MAX_BUTTONS_IN_ROW = 7
MAX_LINK_BUTTONS_IN_ROW = 3


@dataclass(frozen=True)
class Button:
    title: str
    action: str  # callback-команда ядра
    url: str | None = None

    @property
    def is_link(self) -> bool:
        return self.url is not None


@dataclass
class Keyboard:
    rows: list[list[Button]] = field(default_factory=list)

    def row(self, *buttons: Button) -> Keyboard:
        limit = MAX_LINK_BUTTONS_IN_ROW if any(b.is_link for b in buttons) else MAX_BUTTONS_IN_ROW
        if len(buttons) > limit:
            raise ValueError(f"В ряду не больше {limit} кнопок: {[b.title for b in buttons]}")
        self.rows.append(list(buttons))
        return self


@dataclass
class Message:
    text: str
    keyboard: Keyboard | None = None


@dataclass
class ProductCard:
    """Карточка товара «как на сайте».

    В виджете рендерится строкой без фото — там карточек нет по договорённости,
    но данные те же, чтобы ответы каналов не расходились.
    """

    product: Product
    quantity: int = 0
    citation: str | None = None
    keyboard: Keyboard | None = None
    # Заполняется на лету: в выгрузке 1С изображений нет, они добираются с сайта.
    image: str | None = None
    # Тот же снимок, уже лежащий у нас на диске. Telegram не может забрать
    # картинку с vdm.ru сам, поэтому файл ему нужнее адреса.
    image_path: str | None = None


@dataclass
class ProductList:
    title: str
    cards: list[ProductCard]
    total_found: int = 0
    keyboard: Keyboard | None = None


@dataclass
class OrderSummary:
    lines: list[tuple[str, int, int | None]]  # наименование, количество, цена
    total: int
    note: str | None = None
    keyboard: Keyboard | None = None


Response = Message | ProductCard | ProductList | OrderSummary


def price_text(value: int | None) -> str:
    """Цена в человеческом виде. Пустая цена — не ноль, а «по запросу»."""
    if value is None:
        return "цена по запросу"
    return f"{value:,}".replace(",", " ") + " ₽"


def stock_text(product: Product) -> str:
    return f"в наличии {product.in_stock} шт." if product.available else "под заказ"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числительного: 1 позиция, 2 позиции, 5 позиций."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many
