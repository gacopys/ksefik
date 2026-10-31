"""Wspólne elementy interfejsu: pasek tytułu, formularze, potwierdzenia, wybór z listy i ekrany list z KSeF."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from ..client import KsefClient, KsefError, OperationPending, parse_dt
from ..codes import IDENTIFIER_TYPES
from ..config import os_error_text
from ..money import fmt_date, parse_date

if TYPE_CHECKING:
    from .app import KsefApp


CONNECTION_LABELS = {
    "online": ("● połączono", "online"),
    "local": ("○ tryb lokalny", "local"),
    "login": ("! zaloguj się", "login"),
}


class ConnectionBadge(Static):
    """Stan połączenia z KSeF w pasku u góry - zmienia się razem ze stanem aplikacji."""

    app: KsefApp

    def on_mount(self) -> None:
        self.watch(self.app, "connection", self._show)

    def _show(self, state: str) -> None:
        label, css = CONNECTION_LABELS.get(state, CONNECTION_LABELS["login"])
        self.update(label)
        self.set_classes(f"connection-{css}")


def topbar(title: str, app: KsefApp) -> ComposeResult:
    profile = app.profile
    with Horizontal(id="topbar"):
        yield Static(title, id="who")
        yield ConnectionBadge(id="connection")
        yield Static(profile.env_label.upper(), id="env-badge", classes=f"env-{profile.env}")


def downloads_dir() -> Path:
    for name in ("Pobrane", "Downloads"):
        if (path := Path.home() / name).is_dir():
            return path
    return Path.home()


def short_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def safe_name(text: str) -> str:
    return re.sub(r"[^\w.-]+", "-", text).strip("-") or "plik"


@dataclass(frozen=True)
class Saved:
    """Wynik zapisu pliku: ścieżka, a przy niepowodzeniu opis błędu (nic nie zostało nadpisane)."""

    path: Path
    error: str = ""
    existing: bool = False  # identyczny plik już tam był - nie zapisywano drugiej kopii

    @property
    def ok(self) -> bool:
        return not self.error


def save_download(name: str, data: bytes, folder: Path | None = None) -> Saved:
    """Zapisuje plik w folderze (domyślnie Pobrane). Nigdy nie nadpisuje innego pliku: dokłada „ (2)”, „ (3)”…"""
    base = (folder or downloads_dir()) / safe_name(name)
    path, n = base, 1
    while path.exists():
        with suppress(OSError):
            if path.read_bytes() == data:
                return Saved(path, existing=True)
        n += 1
        path = base.with_name(f"{base.stem} ({n}){base.suffix}")
    try:
        with path.open("xb") as file:
            file.write(data)
    except OSError as exc:
        if not isinstance(exc, FileExistsError):
            with suppress(OSError):
                path.unlink()  # np. niepełny plik po zapełnieniu dysku
        return Saved(path, error=os_error_text(exc))
    return Saved(path)


def check_folder(value: str) -> str | None:
    path = Path(value).expanduser()
    if not path.is_dir():
        return "nie ma takiego folderu"
    if not os.access(path, os.W_OK | os.X_OK):
        return "brak uprawnień do zapisu w tym folderze"
    return None


def _clipboard_commands() -> list[list[str]]:
    """Programy schowka systemowego dostępne w tej sesji (w kolejności prób)."""
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform == "win32":
        commands = [["clip"]]
    else:
        commands = []
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(["wl-copy"])
        if os.environ.get("DISPLAY"):
            commands += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
        if "microsoft" in platform.release().lower():
            commands.append(["clip.exe"])  # WSL
    return [command for command in commands if shutil.which(command[0])]


def system_clipboard(text: str) -> bool:
    """Kopiuje do schowka systemowego. True - program schowka potwierdził przyjęcie tekstu."""
    for command in _clipboard_commands():
        try:
            subprocess.run(
                command,
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return True
    return False


def can_open_browser() -> bool:
    """Czy da się otworzyć przeglądarkę. Bez sesji graficznej (np. SSH) przeglądarka tekstowa zajęłaby terminal."""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def draft_saved_text(when: datetime | None = None) -> str:
    return (
        f"Szkic zapisany lokalnie o {(when or datetime.now()):%H:%M}. Nie został wysłany do KSeF.\n"
        "Znajdziesz go w zakładce Szkice."
    )


def fmt_dt(value: str | datetime | None, *, seconds: bool = False) -> str:
    """Data i godzina z API (UTC) w czasie lokalnym."""
    if not value:
        return ""
    if isinstance(value, str):
        value = parse_dt(value)
        if value is None:
            return ""
    return value.astimezone().strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def fmt_ident(identifier: dict[str, Any] | None) -> str:
    """{"type": "Nip", "value": "123"} -> "NIP 123"."""
    if not identifier:
        return ""
    kind = IDENTIFIER_TYPES.get(identifier.get("type", ""), identifier.get("type", ""))
    value = identifier.get("value") or ""
    if identifier.get("type") == "Fingerprint" and len(value) > 16:
        value = value[:16] + "…"
    return f"{kind} {value}".strip()


def person_name(item: dict[str, Any]) -> str:
    person = item.get("subjectPersonDetails") or {}
    if person:
        return f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()
    entity = item.get("subjectEntityDetails") or {}
    return entity.get("fullName", "")


def error_text(exc: KsefError, requires: str = "") -> str:
    text = f"[b $text-error]{escape(exc.message)}[/]"
    if exc.details:
        text += "\n" + "\n".join(f"• {escape(d)}" for d in exc.details)
    if exc.http_status == 403 and requires:
        text += f"\n\n[$foreground-muted]{escape(requires)}[/]"
    return text


MIN_SIZE = (80, 24)


def too_small(size: tuple[int, int]) -> bool:
    return size[0] < MIN_SIZE[0] or size[1] < MIN_SIZE[1]


class TooSmallScreen(ModalScreen[None]):
    """Zasłona przy zbyt małym oknie terminala - nic pod nią nie jest tracone, znika po powiększeniu okna."""

    def compose(self) -> ComposeResult:
        yield Static("", id="too-small")

    def on_mount(self) -> None:
        self.update_size()

    def on_screen_resume(self) -> None:
        # Odsłonięta zasłona (ekran nad nią zamknięto) przy wystarczającym oknie nie jest już potrzebna.
        if not too_small(self.app.size):
            self.dismiss()
        else:
            self.update_size()

    def update_size(self, size: tuple[int, int] | None = None) -> None:
        width, height = size or self.app.size
        self.query_one("#too-small", Static).update(
            f"[b]Powiększ okno terminala[/b]\n\nPotrzeba co najmniej {MIN_SIZE[0]}×{MIN_SIZE[1]} znaków, "
            f"jest {width}×{height}.\n[$foreground-muted]Wpisane dane są zachowane.[/]"
        )


# --------------------------------------------------------------------- formularze


@dataclass(frozen=True)
class Filled:
    """Wynik przycisku przy polu: wartości innych pól, informacja pod polami albo opis błędu."""

    values: dict[str, Any]
    note: str = ""
    error: str = ""


@dataclass(frozen=True)
class FieldAction:
    """Przycisk obok pola, który uzupełnia inne pola (np. dane firmy po NIP). `busy` - etykieta w trakcie."""

    label: str
    run: Callable[[Any], Awaitable[Filled]]
    busy: str = "Pobieram…"


@dataclass
class FormField:
    key: str
    label: str
    kind: str = "text"  # text | password | select | checkbox | multi | date | area
    value: Any = None
    options: Sequence[tuple[str, Any]] = ()
    required: bool = False
    check: Callable[[Any], str | None] | None = None  # zwraca opis błędu albo None
    placeholder: str = ""
    hint: str = ""
    max_length: int = 0
    restrict: str | None = None
    show_if: tuple[str, Iterable[Any]] | None = None  # (klucz innego pola, wartości, przy których pole widać)
    action: FieldAction | None = None


class FormScreen(ModalScreen[dict[str, Any] | None]):
    """Okno z formularzem. Zwraca słownik wartości (daty jako date, pola wyboru jako bool) albo None.

    Enter w polu przechodzi do następnego pola, a po ostatnim - na przycisk zatwierdzenia (bez naciskania go):
    formularz nadający uprawnienia czy generujący token nie może zadziałać od przypadkowego Entera.
    `submit_on_enter` - dla formularzy z jednym polem (np. numer KSeF do pobrania). `local` - zapis lokalny
    (np. profil), który można też zatwierdzić Ctrl+S.
    `on_submit` - operacja w KSeF wykonywana przy otwartym formularzu: zwraca None (sukces - okno się zamyka)
    albo opis błędu, który pojawia się pod polami, a wpisane dane zostają do poprawienia.
    `review` - przegląd przed operacją (np. kto dostanie jakie uprawnienia): tekst okna potwierdzenia.
    """

    BINDINGS = [Binding("escape", "cancel", "Anuluj"), Binding("ctrl+s", "submit", "Zapisz", show=False)]

    def __init__(
        self,
        title: str,
        fields: list[FormField],
        *,
        intro: str = "",
        submit: str = "Zatwierdź",
        validate: Callable[[dict[str, Any]], tuple[str, str] | None] | None = None,
        wide: bool = False,
        submit_on_enter: bool = False,
        local: bool = False,
        back: str = "",
        on_submit: Callable[[dict[str, Any]], Awaitable[str | None]] | None = None,
        review: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.fields = fields
        self.intro = intro
        self.submit_label = submit
        self.validate = validate
        self.wide = wide
        self.submit_on_enter = submit_on_enter
        self.local = local
        self.back = back  # etykieta przycisku powrotu (formularz wieloetapowy) - wynik {BACK: True, …}
        self.on_submit = on_submit
        self.review = review
        self.busy = False  # trwa operacja z on_submit
        self.carried: dict[str, Any] = {}  # wartości z przycisku przy polu dla pól z innego kroku (np. rachunek)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "submit":
            return self.local  # Ctrl+S tylko dla zapisu lokalnego - nigdy dla operacji w KSeF
        return True

    def compose(self) -> ComposeResult:
        # Okno przewija się w całości, a tytuł, komunikat błędu i przyciski są przypięte - widać je w każdym rozmiarze.
        with VerticalScroll(classes="dialog form-dialog" + (" wide" if self.wide else "")):
            yield Static(f"[b]{escape(self.title_text)}[/b]", classes="dialog-title")
            if self.intro:
                yield Static(self.intro, classes="dialog-text")
            for f in self.fields:
                with Vertical(classes="form-field", id=f"row-{f.key}"):
                    if f.kind != "checkbox":
                        yield Label(f.label + (" *" if f.required else ""), classes="field-label")
                    if f.action:
                        with Horizontal(classes="field-with-action"):
                            yield self._widget(f)
                            yield Button(f.action.label, id=f"act-{f.key}", classes="field-action", compact=True)
                    else:
                        yield self._widget(f)
                    if f.hint:
                        yield Static(f"[$foreground-muted]{f.hint}[/]", classes="form-field-hint")
            with Vertical(classes="dialog-footer"):
                yield Static("", id="form-error")
                with Horizontal(classes="dialog-buttons"):
                    if self.back:
                        yield Button(self.back, id="back")
                    yield Button("Anuluj", id="cancel")
                    yield Button(self.submit_label, variant="primary", id="submit")

    def _widget(self, f: FormField) -> Widget:
        wid = f"f-{f.key}"
        if f.kind == "select":
            value = f.value if any(v == f.value for _, v in f.options) else Select.NULL
            blank = not f.required or value is Select.NULL
            return Select(list(f.options), value=value, allow_blank=blank, id=wid, compact=True)
        if f.kind == "checkbox":
            return Checkbox(f.label, bool(f.value), id=wid, compact=True)
        if f.kind == "multi":
            selected = set(f.value or [])
            return SelectionList[Any](*[(label, v, v in selected) for label, v in f.options], id=wid)
        if f.kind == "area":
            return TextArea(str(f.value or ""), id=wid, classes="form-area")
        value = fmt_date(f.value) if isinstance(f.value, date) else ("" if f.value is None else str(f.value))
        kwargs: dict[str, Any] = {"placeholder": f.placeholder or ("DD.MM.RRRR" if f.kind == "date" else "")}
        if f.max_length:
            kwargs["max_length"] = f.max_length
        if f.restrict:
            kwargs["restrict"] = f.restrict
        return Input(value, id=wid, password=f.kind == "password", compact=True, **kwargs)

    def on_mount(self) -> None:
        self._update_visibility()
        first = next((f for f in self.fields if self._visible(f)), None)
        if first:
            self.query_one(f"#f-{first.key}").focus()

    def _raw(self, f: FormField) -> Any:
        widget = self.query_one(f"#f-{f.key}")
        if isinstance(widget, Select):
            return None if widget.value is Select.NULL else widget.value
        if isinstance(widget, Checkbox):
            return widget.value
        if isinstance(widget, SelectionList):
            return list(widget.selected)
        if isinstance(widget, TextArea):
            return widget.text.strip()
        assert isinstance(widget, Input)
        return widget.value.strip()

    def _visible(self, f: FormField) -> bool:
        if not f.show_if:
            return True
        other = next((x for x in self.fields if x.key == f.show_if[0]), None)
        return other is not None and self._visible(other) and self._raw(other) in set(f.show_if[1])

    def _update_visibility(self) -> None:
        for f in self.fields:
            self.query_one(f"#row-{f.key}").display = self._visible(f)

    @on(Select.Changed)
    @on(Checkbox.Changed)
    def _changed(self) -> None:
        self._update_visibility()

    @on(Input.Submitted)
    def _input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.submit_on_enter:
            self.action_submit()
            return
        widgets = [self.query_one(f"#f-{f.key}") for f in self.fields if self._visible(f)]
        position = next((n for n, w in enumerate(widgets) if w is event.input), len(widgets) - 1)
        following = widgets[position + 1 :]
        (following[0] if following else self.query_one("#submit", Button)).focus()

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        if self.busy:
            return
        values: dict[str, Any] = {}
        for f in self.fields:
            if not self._visible(f):
                values[f.key] = None
                continue
            raw = self._raw(f)
            problem = None
            if f.kind == "date" and raw:
                parsed = parse_date(raw)
                problem = None if parsed else "podaj datę w formacie DD.MM.RRRR"
                raw = parsed
            if problem is None and f.required and (raw is None or raw == "" or raw == []):
                problem = "to pole jest wymagane"
            if problem is None and f.check and raw not in (None, "", []):
                problem = f.check(raw)
            if problem:
                self._error(f.key, problem)
                return
            values[f.key] = raw if raw != "" else None
        if self.validate and (problem := self.validate(values)):
            self._error(*problem)
            return
        self.query_one("#form-error", Static).update("")
        if self.review is not None:

            def reviewed(ok: bool | None) -> None:
                if ok:
                    self._accept(values)

            self.app.push_screen(
                ConfirmScreen(
                    f"Sprawdź: {self.title_text}",
                    self.review(values),
                    confirm=self.submit_label,
                    cancel="Popraw",
                    danger=False,
                    focus_cancel=True,
                ),
                reviewed,
            )
            return
        self._accept(values)

    def _accept(self, values: dict[str, Any]) -> None:
        values = {**self.carried, **values}
        if self.on_submit is not None:
            self._submit(values)
        else:
            self.dismiss(values)

    @work(exclusive=True, group="form-submit")
    async def _submit(self, values: dict[str, Any]) -> None:
        """Operacja przy otwartym oknie: przyciski są zablokowane, a błąd nie gubi wpisanych danych."""
        assert self.on_submit is not None
        button = self.query_one("#submit", Button)
        label = button.label
        self.busy = True
        self._lock(True)
        button.label = "Czekam na KSeF…"
        error = self.query_one("#form-error", Static)
        error.update("[$text-accent]⟳ Wysyłam do KSeF…[/]")
        try:
            problem = await self.on_submit(values)
        finally:
            self.busy = False
            button.label = label
            self._lock(False)
        if problem is None:
            self.dismiss(values)
            return
        error.update(f"[$text-error]{escape(problem)}[/]\n[$foreground-muted]Wpisane dane są zachowane.[/]")
        button.focus()

    def _lock(self, locked: bool) -> None:
        for button in self.query(".dialog-buttons Button"):
            button.disabled = locked

    @on(Button.Pressed, ".field-action")
    def _field_action(self, event: Button.Pressed) -> None:
        event.stop()
        field = next(f for f in self.fields if f"act-{f.key}" == event.button.id)
        raw = self._raw(field)
        problem = "najpierw wpisz wartość" if raw in (None, "") else field.check(raw) if field.check else None
        if problem:
            self._error(field.key, problem)
            return
        self._run_action(field, raw)

    @work(exclusive=True, group="field-action")
    async def _run_action(self, field: FormField, raw: Any) -> None:
        assert field.action is not None
        button = self.query_one(f"#act-{field.key}", Button)
        message = self.query_one("#form-error", Static)
        button.disabled, button.label = True, field.action.busy
        message.update(f"[$text-accent]⟳ {escape(field.action.busy)}[/]")
        try:
            result = await field.action.run(raw)
        finally:
            button.disabled, button.label = False, field.action.label
        if result.error:
            message.update(f"[$text-error]{escape(result.error)}[/]")
            return
        for key, value in result.values.items():
            target = next((f for f in self.fields if f.key == key), None)
            if target is None:
                self.carried[key] = value
                continue
            widget = self.query_one(f"#f-{key}")
            if isinstance(widget, Input):
                widget.value = "" if value is None else str(value)
            elif isinstance(widget, (Select, Checkbox)):
                widget.value = value
        message.update(result.note)

    def _error(self, key: str, problem: str) -> None:
        field = next((f for f in self.fields if f.key == key), None)
        label = f"{field.label}: " if field else ""
        self.query_one("#form-error", Static).update(f"[$text-error]{escape(label)}{escape(problem)}[/]")
        if field:
            self.query_one(f"#f-{key}").focus()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if self.busy:
            self.notify("Poczekaj na odpowiedź KSeF - wniosek mógł już do niego dotrzeć.", timeout=4)
            return
        if self.back:
            self._back()  # w kolejnym kroku Esc wraca do poprzedniego, a nie przerywa całości
            return
        self.dismiss(None)

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        """Powrót do poprzedniego kroku z wpisanymi wartościami (bez sprawdzania - mogą być niedokończone)."""
        values: dict[str, Any] = {f.key: self._raw(f) for f in self.fields}
        self.dismiss({BACK: True, **self.carried, **values})


BACK = "__back__"
OUTCOME = "__outcome__"  # wynik operacji z on_submit w wartościach zamkniętego formularza


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Anuluj")]

    def __init__(
        self,
        title: str,
        message: str,
        *,
        confirm: str = "Tak",
        cancel: str = "Anuluj",
        danger: bool = True,
        focus_cancel: bool | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.confirm = confirm
        self.cancel = cancel
        self.danger = danger
        # Domyślnie (Enter) wybieramy bezpieczną opcję: przy groźnych operacjach i tam, gdzie wywołujący tak chce.
        self.focus_cancel = danger if focus_cancel is None else focus_cancel

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog confirm-dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/b]", classes="dialog-title")
            yield Static(self.message, classes="dialog-text")
            with Horizontal(classes="dialog-buttons dialog-footer"):
                yield Button(self.cancel, id="cancel")
                yield Button(self.confirm, variant="error" if self.danger else "primary", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#cancel" if self.focus_cancel else "#confirm", Button).focus()

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class ChoiceScreen(ModalScreen[Any]):
    """Wybór jednej z opcji: [(etykieta, opis, wartość)]."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj")]

    def __init__(self, title: str, choices: list[tuple[str, str, Any]], *, intro: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.choices = choices
        self.intro = intro

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog choice-dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/b]", classes="dialog-title")
            if self.intro:
                yield Static(self.intro, classes="dialog-text")
            options = []
            for n, (label, description, _) in enumerate(self.choices):
                prompt = Text.from_markup(f"[b]{escape(label)}[/b]")
                if description:
                    prompt.append("\n" + description, style="dim")
                options.append(Option(prompt, id=str(n)))
            yield OptionList(*options, id="choices")

    def on_mount(self) -> None:
        self.call_after_refresh(self._fit)

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit)

    def _fit(self) -> None:
        """Lista opcji przewija się wewnątrz okna, gdy cała się nie mieści (np. w terminalu 80×24)."""
        dialog = self.query_one(".choice-dialog")
        choices = self.query_one("#choices", OptionList)
        chrome = dialog.outer_size.height - choices.outer_size.height
        choices.styles.max_height = max(4, self.size.height - 2 - chrome)

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.choices[int(event.option.id or 0)][2])

    def action_cancel(self) -> None:
        self.dismiss(None)


def _own_binding(node: object, binding: Binding) -> bool:
    """Czy skrót zdefiniowała ta aplikacja (klasa z ksefik), a nie Textual."""
    for cls in type(node).__mro__:
        if cls.__module__.startswith("ksefik"):
            for own in cls.__dict__.get("BINDINGS", []):
                if isinstance(own, Binding) and (own.key, own.action) == (binding.key, binding.action):
                    return True
    return False


class HelpScreen(ModalScreen[None]):
    """Pomoc (F1): do czego służy ekran i wszystkie jego skróty - także te, których nie ma w stopce."""

    BINDINGS = [Binding("escape", "close", "Zamknij"), Binding("f1", "close", "Zamknij", show=False)]

    def __init__(self, screen: Screen) -> None:
        super().__init__()
        self.text = getattr(screen, "HELP", "") or getattr(screen, "DESCRIPTION", "")
        self.keys: list[tuple[str, str, bool]] = []  # (klawisz, opis, dostępny teraz)
        seen: dict[str, int] = {}
        for key, active in screen.active_bindings.items():
            binding = active.binding
            # Tylko skróty tej aplikacji - bez wbudowanych w Textual (np. „Cursor down” tabeli, Ctrl+Q).
            if not binding.description or active.node is self.app or not _own_binding(active.node, binding):
                continue
            display = self.app.get_key_display(binding) if binding.key_display is None else binding.key_display
            if binding.description in seen:  # ten sam opis pod kilkoma klawiszami (np. ← i →)
                n = seen[binding.description]
                shown, description, enabled = self.keys[n]
                shown = shown if display in shown else f"{shown} {display}"
                self.keys[n] = (shown, description, enabled or active.enabled)
                continue
            seen[binding.description] = len(self.keys)
            self.keys.append((display or key, binding.description, active.enabled))

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog help-dialog"):
            yield Static("[b]Pomoc[/b]", classes="dialog-title")
            if self.text:
                yield Static(self.text, classes="dialog-text")
            width = max((len(k) for k, _, _ in self.keys), default=2)
            lines = []
            for key, description, enabled in self.keys:
                label = f"[b $accent]{escape(key.ljust(width))}[/]  {escape(description)}"
                lines.append(
                    label
                    if enabled
                    else f"[$foreground-muted]{escape(key.ljust(width))}  {escape(description)} (teraz niedostępne)[/]"
                )
            palette = self.app.get_key_display(Binding("ctrl+p", "command_palette"))
            lines.append(f"[b $accent]{palette.ljust(width)}[/]  wszystkie polecenia - wpisz, czego szukasz")
            yield Static("\n".join(lines), id="help-keys")
            with Horizontal(classes="dialog-buttons dialog-footer"):
                yield Button("Zamknij", variant="primary", id="close")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


SELECT_HINT = "zaznacz go myszą z wciśniętym Shift i skopiuj skrótem terminala (zwykle Ctrl+Shift+C)"


class ValueScreen(ModalScreen[None]):
    """Tekst do skopiowania ręcznie - gdy schowek albo przeglądarka nie działają (np. przez SSH)."""

    BINDINGS = [Binding("escape", "close", "Zamknij")]

    app: KsefApp

    def __init__(self, title: str, intro: str, value: str, *, copy: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.intro = intro
        self.value = value
        self.copy = copy  # etykieta przycisku kopiowania (puste - bez przycisku)

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog secret-dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/b]", classes="dialog-title")
            yield Static(self.intro, classes="dialog-text")
            yield Static(escape(self.value), id="secret-value")
            with Horizontal(classes="dialog-buttons dialog-footer"):
                if self.copy:
                    yield Button(self.copy, variant="primary", id="copy")
                yield Button("Zamknij", id="close")

    def on_mount(self) -> None:
        self.query_one("#copy" if self.copy else "#close", Button).focus()

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        self.app.copy_to_clipboard(self.value)
        if self.app.clipboard_confirmed:
            self.notify("Skopiowano do schowka.", timeout=3)
        else:
            self.notify(f"Nie da się sprawdzić, czy schowek działa - {SELECT_HINT}.", severity="warning", timeout=8)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------- listy z KSeF


@dataclass(frozen=True)
class Outcome:
    """Wynik operacji w KSeF: "ok" (value - odpowiedź), "pending" (wniosek w toku, message - jego numer),
    "failed" (message - opis błędu) albo "cancelled" (bez połączenia - nic nie wysłano)."""

    status: str
    value: Any = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class ListScreen(Screen):
    """Lista obiektów pobranych z KSeF z panelem szczegółów. Podklasy definiują kolumny i sposób pobierania."""

    TITLE_TEXT = ""
    DESCRIPTION = ""
    COLUMNS: list[tuple[str, int | None]] = []
    EMPTY = "Brak pozycji."
    REQUIRES = ""  # podpowiedź przy błędzie 403
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Wróć"),
        Binding("r", "reload", "Odśwież"),
        Binding("s", "check_operations", "Sprawdź status"),
    ]

    app: KsefApp

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self.pending_operations: dict[str, str] = {}  # numer wniosku -> opis operacji

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "check_operations":
            return bool(self.pending_operations)
        return True

    def compose(self) -> ComposeResult:
        yield from topbar(f"[b]{escape(self.TITLE_TEXT)}[/b]", self.app)
        if self.DESCRIPTION:
            yield Static(self.DESCRIPTION, classes="screen-description")
        yield from self.compose_header()
        yield DataTable(cursor_type="row", zebra_stripes=True, id="list")
        yield Static("", id="list-empty")
        yield Static("", id="list-details")
        yield Static("", id="op-status")
        yield Footer()

    def compose_header(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        table = self.query_one("#list", DataTable)
        for label, width in self.COLUMNS:
            table.add_column(label, width=width)
        self.action_reload()

    def show_outcome(self, text: str) -> None:
        """Trwały opis wyniku ostatniej operacji (powiadomienie znika, ten wiersz zostaje)."""
        status = self.query_one("#op-status", Static)
        status.update(text)
        status.display = bool(text)

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        raise NotImplementedError

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        raise NotImplementedError

    def details(self, item: dict[str, Any]) -> str:
        return ""

    @property
    def selected(self) -> dict[str, Any] | None:
        table = self.query_one("#list", DataTable)
        if not self.items or table.cursor_row >= len(self.items):
            return None
        return self.items[table.cursor_row]

    def _show_message(self, text: str) -> None:
        # Ukryta lista nie może zostać celem skrótów (np. Del - odbierz) - jej wiersze mogą być nieaktualne.
        self.items = []
        self.query_one("#list", DataTable).display = False
        self.query_one("#list-details", Static).display = False
        empty = self.query_one("#list-empty", Static)
        empty.display = True
        empty.update(text)

    @work(exclusive=True, group="list")
    async def action_reload(self) -> None:
        self._show_message("[$foreground-muted]Wczytuję dane z KSeF…[/]")
        client = await self.app.get_client()
        if client is None:
            self._show_message(
                "[b]Wymaga połączenia z KSeF[/b]\n\n"
                "Ta lista jest pobierana z KSeF na bieżąco. Naciśnij [b $accent]r[/], aby się połączyć."
            )
            return
        try:
            items = await self.fetch(client)
        except KsefError as exc:
            self._show_message(error_text(exc, self.REQUIRES))
            return
        self.show_items(items)

    def show_items(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        table = self.query_one("#list", DataTable)
        row = table.cursor_row
        table.clear()
        for item in items:
            table.add_row(*self.row(item))
        if not items:
            self._show_message(self.EMPTY)
            return
        self.query_one("#list-empty", Static).display = False
        table.display = True
        details = self.query_one("#list-details", Static)
        details.display = True
        table.move_cursor(row=min(row, len(items) - 1))
        table.focus()
        self._update_details()

    @on(DataTable.RowHighlighted, "#list")
    def _update_details(self) -> None:
        item = self.selected
        self.query_one("#list-details", Static).update(self.details(item) if item else "")

    async def run_operation(
        self,
        title: str,
        operation: Callable[[KsefClient], Awaitable[Any]],
        *,
        success: str,
        reload: bool = True,
        notify_errors: bool = True,
    ) -> Outcome:
        """Wykonuje operację w KSeF; wynik zostaje opisany pod listą (wywoływać z workera).

        `notify_errors=False` - błąd pokazuje wywołujący (np. formularz pod polami), bez dodatkowego powiadomienia.
        """
        client = await self.app.get_client()
        if client is None:
            return Outcome("cancelled", message="Nie połączono z KSeF - nic nie zostało wysłane.")
        self.show_outcome(f"[$text-accent]⟳ {escape(title)}…[/]")
        try:
            result = await operation(client)
        except OperationPending as exc:
            self.pending_operations[exc.reference] = title
            self.show_outcome(
                f"[$text-warning]{escape(title)}: wniosek jest w trakcie przetwarzania w KSeF "
                f"(numer {escape(exc.reference)}). Nie potwierdzono jeszcze zmiany - naciśnij s, aby sprawdzić.[/]"
            )
            self.refresh_bindings()
            return Outcome("pending", message=exc.reference)
        except KsefError as exc:
            self.show_outcome(f"[$text-error]{escape(title)}: {escape(exc.message)}[/]")
            if notify_errors:
                self.notify(exc.human(), title=title, severity="error", timeout=12)
            return Outcome("failed", message=exc.human())
        self.show_outcome(f"[$text-success]✓ {escape(title)}: {escape(success)}[/]")
        self.notify(success, title=title, timeout=6)
        if reload:
            self.action_reload()
        return Outcome("ok", result)

    def form_operation(
        self,
        title: str | Callable[[dict[str, Any]], str],
        operation: Callable[[KsefClient, dict[str, Any]], Awaitable[Any]],
        *,
        success: str,
    ) -> Callable[[dict[str, Any]], Awaitable[str | None]]:
        """`on_submit` formularza: operacja w KSeF; przy błędzie okno zostaje otwarte z wpisanymi danymi.

        Po sukcesie albo przy wniosku w toku okno się zamyka, a wynik (Outcome) jest w wartościach pod kluczem
        OUTCOME. Stan wniosku w toku widać pod listą i sprawdza się klawiszem s.
        """

        async def submit(values: dict[str, Any]) -> str | None:
            name = title(values) if callable(title) else title
            outcome = await self.run_operation(
                name, lambda client: operation(client, values), success=success, notify_errors=False
            )
            if outcome.status in ("failed", "cancelled"):
                return outcome.message
            values[OUTCOME] = outcome
            return None

        return submit

    @work(exclusive=True, group="op-status")
    async def action_check_operations(self) -> None:
        """Sprawdza wnioski, które przy poprzedniej próbie były jeszcze w trakcie (po ich numerach)."""
        if not self.pending_operations:
            return
        client = await self.app.get_client()
        if client is None:
            return
        lines = []
        for reference, title in list(self.pending_operations.items()):
            try:
                status = (await client.permission_operation(reference))["status"]
            except KsefError as exc:
                lines.append(f"[$text-warning]{escape(title)} ({escape(reference)}): {escape(exc.message)}[/]")
                continue
            if status["code"] == 200:
                del self.pending_operations[reference]
                lines.append(f"[$text-success]✓ {escape(title)}: zakończone ({escape(reference)}).[/]")
            elif status["code"] >= 400:
                del self.pending_operations[reference]
                reason = status.get("description", "")
                lines.append(f"[$text-error]{escape(title)} nie powiodło się: {escape(reason)} ({reference})[/]")
            else:
                lines.append(f"[$text-warning]{escape(title)}: wciąż w trakcie ({escape(reference)}).[/]")
        self.show_outcome("\n".join(lines))
        self.refresh_bindings()
        self.action_reload()


def check_nip(value: str) -> str | None:
    from ..money import nip_valid

    return None if nip_valid(value) else "niepoprawny NIP (zła suma kontrolna)"


def check_pesel(value: str) -> str | None:
    if not re.fullmatch(r"\d{11}", value):
        return "PESEL ma 11 cyfr"
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    control = (10 - sum(int(d) * w for d, w in zip(value, weights, strict=False)) % 10) % 10
    return None if control == int(value[10]) else "niepoprawny PESEL (zła suma kontrolna)"


def check_identifier(kind: str) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        if kind == "Nip":
            return check_nip(value)
        if kind == "Pesel":
            return check_pesel(value)
        if kind == "Fingerprint" and not re.fullmatch(r"[0-9A-Fa-f]{64}", value):
            return "odcisk certyfikatu SHA-256 to 64 znaki szesnastkowe"
        return None

    return check


def check_description(value: str) -> str | None:
    return None if 5 <= len(value) <= 256 else "opis musi mieć od 5 do 256 znaków"
