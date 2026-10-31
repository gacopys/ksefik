"""Uprawnienia w KSeF: przegląd (nadane, otrzymane, własne, role) oraz nadawanie i odbieranie."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.markup import escape
from textual.widgets import Label, Select

from ..client import KsefClient, person_details
from ..codes import (
    AUTHORIZATION_PERMISSIONS,
    ENTITY_PERMISSIONS,
    ENTITY_ROLES,
    IDENTIFIER_TYPES,
    PERMISSION_LABELS,
    PERSON_PERMISSIONS,
)
from .common import (
    ChoiceScreen,
    ConfirmScreen,
    FormField,
    FormScreen,
    ListScreen,
    check_description,
    check_identifier,
    check_nip,
    fmt_dt,
    fmt_ident,
    person_name,
)

if TYPE_CHECKING:
    from .app import KsefApp

PERSON_IDS = [("NIP", "Nip"), ("PESEL", "Pesel"), ("Odcisk certyfikatu (SHA-256)", "Fingerprint")]
INTERNAL_ID = re.compile(r"\d{10}-\d{5}")
NIP_VAT_UE = re.compile(r"\d{10}-[A-Z]{2}[0-9A-Z+*]{2,12}")


def check_internal_id(value: str) -> str | None:
    return None if INTERNAL_ID.fullmatch(value) else "NIP i 5 cyfr, np. 1234567890-00011"


def check_nip_vat_ue(value: str) -> str | None:
    if not NIP_VAT_UE.fullmatch(value.upper()):
        return "NIP, myślnik i numer VAT UE z kodem kraju, np. 1234567890-DE123456789"
    return check_nip(value[:10])


def check_unit(kind: str) -> Callable[[str], str | None]:
    """Identyfikator klienta albo jednostki: NIP albo identyfikator wewnętrzny."""
    return check_nip if kind == "Nip" else check_internal_id


def _permissions(keys: list[str] | None) -> str:
    return ", ".join(PERMISSION_LABELS.get(k, k) for k in keys or [])


def _person(v: dict[str, Any]) -> str:
    ident = v["value"] if v["kind"] != "Fingerprint" else v["value"][:16] + "…"
    return f"{v['first']} {v['last']} ({IDENTIFIER_TYPES.get(v['kind'], v['kind'])} {ident})"


def _recipient(v: dict[str, Any]) -> str:
    """Kogo dotyczy wniosek - do opisu operacji (wynik w toku trafia do właściwej osoby, nie do zaznaczenia)."""
    if v.get("first") or v.get("last"):
        return f"{v.get('first') or ''} {v.get('last') or ''}".strip()
    return v.get("name") or v.get("entity_name") or v.get("eu_name") or v.get("value") or v.get("nip") or ""


def _review(rows: list[tuple[str, str]]) -> str:
    """Przegląd przed nadaniem: kto, co, w czyim imieniu, czy może przekazywać dalej."""
    # Etykieta nad wartością - długie nazwy firm nie zawijają się pod etykiety w wąskim oknie.
    lines = [f"[$foreground-muted]{escape(label)}[/]\n[b]{value}[/b]" for label, value in rows]
    lines.append("[$foreground-muted]Zmiana zacznie działać, gdy KSeF ją potwierdzi (zwykle kilka sekund).[/]")
    return "\n\n".join(lines)


@dataclass(frozen=True)
class View:
    label: str
    fetch: Callable[[KsefClient], Awaitable[list[dict[str, Any]]]]
    row: Callable[[dict[str, Any]], tuple[str, str, str]]  # (kto, identyfikator, uprawnienie)
    revoke: str | None = None  # "common" | "authorization" | None


def _scope(item: dict[str, Any], key: str = "permissionScope") -> str:
    value = item.get(key, "")
    return PERMISSION_LABELS.get(value, value)


VIEWS: dict[str, View] = {
    "granted": View(
        "Nadane osobom i podmiotom",
        lambda c: c.query_person_permissions("PermissionsGrantedInCurrentContext"),
        lambda i: (person_name(i), fmt_ident(i.get("authorizedIdentifier")), _scope(i)),
        "common",
    ),
    "in_context": View(
        "Obowiązujące w tym kontekście",
        lambda c: c.query_person_permissions("PermissionsInCurrentContext"),
        lambda i: (person_name(i), fmt_ident(i.get("authorizedIdentifier")), _scope(i)),
        "common",
    ),
    "personal": View(
        "Moje uprawnienia",
        lambda c: c.query_personal_permissions(),
        lambda i: (person_name(i) or "ja", fmt_ident(i.get("contextIdentifier")), _scope(i)),
    ),
    "authorizations": View(
        "Uprawnienia podmiotowe - nadane",
        lambda c: c.query_authorizations("Granted"),
        lambda i: (
            (i.get("subjectEntityDetails") or {}).get("fullName", ""),
            fmt_ident(i.get("authorizedEntityIdentifier")),
            _scope(i, "authorizationScope"),
        ),
        "authorization",
    ),
    "authorizations_received": View(
        "Uprawnienia podmiotowe - otrzymane",
        lambda c: c.query_authorizations("Received"),
        lambda i: (
            (i.get("subjectEntityDetails") or {}).get("fullName", ""),
            fmt_ident(i.get("authorizingEntityIdentifier")),
            _scope(i, "authorizationScope"),
        ),
    ),
    "entities": View(
        "Otrzymane od innych firm (obsługa ich faktur)",
        lambda c: c.query_entity_permissions(),
        lambda i: ("", fmt_ident(i.get("contextIdentifier")), _scope(i)),
    ),
    "subunits": View(
        "Administratorzy jednostek podrzędnych",
        lambda c: c.query_subunit_permissions(),
        lambda i: (
            person_name(i),
            fmt_ident(i.get("authorizedIdentifier")),
            f"Administrator: {i.get('subunitName') or fmt_ident(i.get('subunitIdentifier'))}",
        ),
        "common",
    ),
    "eu": View(
        "Podmioty unijne (administratorzy i reprezentanci)",
        lambda c: c.query_eu_entity_permissions(),
        lambda i: (i.get("euEntityName", ""), i.get("vatUeIdentifier", ""), _scope(i)),
        "common",
    ),
    "roles": View(
        "Role firmy w KSeF",
        lambda c: c.query_entity_roles(),
        lambda i: (
            ENTITY_ROLES.get(i.get("role", ""), i.get("role", "")),
            fmt_ident(i.get("parentEntityIdentifier")),
            "rola",
        ),
    ),
    "subordinate": View(
        "Podmioty podrzędne (JST, grupa VAT)",
        lambda c: c.query_subordinate_entities(),
        lambda i: (
            ENTITY_ROLES.get(i.get("role", ""), i.get("role", "")),
            fmt_ident(i.get("subordinateEntityIdentifier")),
            "podmiot podrzędny",
        ),
    ),
}


def _person_fields(permissions: list[str] | None = None, *, fingerprint: bool = True) -> list[FormField]:
    kinds = PERSON_IDS if fingerprint else PERSON_IDS[:2]
    fields = [
        FormField("kind", "Identyfikator osoby", "select", "Pesel", kinds, required=True),
        FormField("value", "NIP, PESEL albo odcisk certyfikatu", required=True),
        FormField("first", "Imię", required=True),
        FormField("last", "Nazwisko", required=True),
    ]
    if fingerprint:
        fp = ("kind", ["Fingerprint"])
        fields += [
            FormField(
                "fp_id_type",
                "PESEL lub NIP osoby",
                "select",
                "",
                [("brak - podam dokument tożsamości", ""), ("PESEL", "Pesel"), ("NIP", "Nip")],
                show_if=fp,
            ),
            FormField("fp_id", "Numer", show_if=("fp_id_type", ["Pesel", "Nip"])),
            FormField("birth", "Data urodzenia", "date", show_if=("fp_id_type", [None, ""])),
            FormField("doc_type", "Rodzaj dokumentu", placeholder="np. paszport", show_if=("fp_id_type", [None, ""])),
            FormField("doc_number", "Seria i numer dokumentu", show_if=("fp_id_type", [None, ""])),
            FormField(
                "doc_country", "Kraj wydania (kod)", value="PL", max_length=2, show_if=("fp_id_type", [None, ""])
            ),
        ]
    if permissions:
        fields.append(
            FormField(
                "permissions", "Uprawnienia", "multi", ["InvoiceRead"], [(PERMISSION_LABELS[p], p) for p in permissions]
            )
        )
    fields.append(FormField("description", "Opis (widoczny w KSeF)", required=True, check=check_description))
    return fields


def _validate_person(values: dict[str, Any]) -> tuple[str, str] | None:
    if problem := check_identifier(values["kind"])(values["value"]):
        return "value", problem
    if values["kind"] == "Fingerprint" and not values.get("fp_id_type"):
        for key in ("birth", "doc_type", "doc_number", "doc_country"):
            if not values.get(key):
                return key, "podaj dane dokumentu tożsamości albo PESEL/NIP osoby"
        if values["birth"] >= date.today():
            return "birth", "data urodzenia musi być wcześniejsza niż dzisiaj"
        if not re.fullmatch(r"[A-Za-z]{2}", values["doc_country"]):
            return "doc_country", "dwuliterowy kod kraju, np. PL"
    if values["kind"] == "Fingerprint" and values.get("fp_id_type"):
        if not values.get("fp_id"):
            return "fp_id", "podaj numer"
        if problem := check_identifier(values["fp_id_type"])(values["fp_id"]):
            return "fp_id", problem
    if "permissions" in values and not values["permissions"]:
        return "permissions", "wybierz co najmniej jedno uprawnienie"
    return None


def _subject_details(values: dict[str, Any]) -> dict[str, Any]:
    if values["kind"] != "Fingerprint":
        return person_details(values["first"], values["last"])
    name = {"firstName": values["first"], "lastName": values["last"]}
    if values.get("fp_id_type"):
        identifier = {"type": values["fp_id_type"], "value": values["fp_id"]}
        return {
            "subjectDetailsType": "PersonByFingerprintWithIdentifier",
            "personByFpWithId": {**name, "identifier": identifier},
        }
    document = {"type": values["doc_type"], "number": values["doc_number"], "country": values["doc_country"].upper()}
    return {
        "subjectDetailsType": "PersonByFingerprintWithoutIdentifier",
        "personByFpNoId": {**name, "birthDate": values["birth"].isoformat(), "idDocument": document},
    }


GRANT_TYPES = [
    ("Osobie fizycznej", "pracownik, księgowa - praca w KSeF w imieniu firmy", "person"),
    ("Innej firmie (obsługa faktur)", "np. biuro rachunkowe - przeglądanie i wystawianie faktur", "entity"),
    ("Uprawnienie podmiotowe", "samofakturowanie, faktury RR, przedstawiciel podatkowy, PEF", "authorization"),
    ("Pośrednio - dla klientów", "biuro rachunkowe przekazuje uprawnienie swojemu pracownikowi", "indirect"),
    ("Administrator jednostki podrzędnej", "identyfikator wewnętrzny albo jednostka JST/grupy VAT", "subunit"),
    ("Administrator podmiotu unijnego", "samofakturowanie przez podmiot z UE (kontekst NIP-VAT UE)", "eu_admin"),
    ("Reprezentant podmiotu unijnego", "w kontekście NIP-VAT UE", "eu_representative"),
]


class PermissionsScreen(ListScreen):
    TITLE_TEXT = "Uprawnienia"
    COLUMNS = [("Kto", 28), ("Identyfikator", 24), ("Uprawnienie", 30), ("Opis", 30), ("Od", 16)]
    EMPTY = "Brak uprawnień w tym widoku."
    REQUIRES = (
        "Przeglądanie uprawnień wymaga uprawnienia CredentialsRead lub CredentialsManage - np. logowania "
        "właściciela firmy podpisem kwalifikowanym albo tokenem z tymi uprawnieniami."
    )
    BINDINGS = [
        *ListScreen.BINDINGS,
        Binding("n", "grant", "Nadaj uprawnienia"),
        Binding("delete", "revoke", "Odbierz", key_display="Del"),
    ]

    app: KsefApp

    def compose_header(self) -> ComposeResult:
        with Horizontal(classes="list-filters"):
            yield Label("Widok", classes="filter-label first")
            options = [(view.label, key) for key, view in VIEWS.items()]
            # Administrator podmiotu z UE (kontekst NIP-VAT UE) zarządza tylko uprawnieniami tego podmiotu.
            start = "eu" if self.app.profile.context_type == "NipVatUe" else "granted"
            yield Select(options, value=start, allow_blank=False, id="view", compact=True)

    @property
    def view(self) -> View:
        return VIEWS[str(self.query_one("#view", Select).value)]

    @on(Select.Changed, "#view")
    def _view_changed(self) -> None:
        self.refresh_bindings()
        self.action_reload()

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        return await self.view.fetch(client)

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        who, ident, scope = self.view.row(item)
        inactive = item.get("permissionState") == "Inactive"
        style = "dim" if inactive else ""
        return (
            Text(who or "—", style=style),
            Text(ident, style=style),
            Text(scope + (" (nieaktywne)" if inactive else ""), style=style),
            Text(item.get("description", ""), style=style),
            fmt_dt(item.get("startDate")),
        )

    def details(self, item: dict[str, Any]) -> str:
        parts = []
        if author := item.get("authorIdentifier"):
            parts.append(f"[$foreground-muted]nadał:[/] {escape(fmt_ident(author))}")
        if target := item.get("targetIdentifier"):
            parts.append(f"[$foreground-muted]dla klienta:[/] {escape(fmt_ident(target))}")
        if "canDelegate" in item:
            parts.append(f"[$foreground-muted]dalsze przekazywanie:[/] {'tak' if item['canDelegate'] else 'nie'}")
        if item.get("id"):
            parts.append(f"[$foreground-muted]id:[/] {item['id']}")
        return "   ".join(parts)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "revoke" and self.is_mounted:
            return self.view.revoke is not None
        return super().check_action(action, parameters)

    # --------------------------------------------------------------- odbieranie

    def action_revoke(self) -> None:
        item = self.selected
        view = self.view
        if item is None or view.revoke is None or not item.get("id"):
            return
        who, ident, scope = view.row(item)
        # Cel ustalamy teraz - wynik (także późniejszy, gdy wniosek będzie w toku) dotyczy tego wiersza.
        rows = [("Kto", escape(f"{who} {ident}".strip() or "—")), ("Uprawnienie", escape(scope))]
        if target := item.get("targetIdentifier"):
            rows.append(("Dla klienta", escape(fmt_ident(target))))
        if context := item.get("contextIdentifier"):
            rows.append(("Kontekst", escape(fmt_ident(context))))
        if item.get("canDelegate"):
            rows.append(("Przekazane dalej", "[$text-warning]osoby, którym je przekazano, też je stracą[/]"))
        if description := item.get("description"):
            rows.append(("Opis", escape(f"„{description}”")))
        width = max(len(label) for label, _ in rows)
        text = "\n".join(f"[$foreground-muted]{escape(label.ljust(width))}[/]  {value}" for label, value in rows)
        title = f"Odebranie uprawnienia: {who or ident}"

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._revoke(item["id"], view.revoke == "authorization", title)

        self.app.push_screen(
            ConfirmScreen(
                "Odebrać uprawnienie?",
                text + "\n\nTej operacji nie można cofnąć - uprawnienie trzeba będzie nadać od nowa.",
                confirm="Odbierz",
            ),
            confirmed,
        )

    @work(exclusive=True, group="op")
    async def _revoke(self, permission_id: str, authorization: bool, title: str = "Odebranie uprawnienia") -> None:
        async def revoke(client: KsefClient) -> Any:
            ref = await (client.revoke_authorization if authorization else client.revoke_permission)(permission_id)
            return await client.wait_for_permission_operation(ref)

        await self.run_operation(title, revoke, success="Uprawnienie odebrane.")

    # ----------------------------------------------------------------- nadawanie

    def action_grant(self) -> None:
        self.app.push_screen(ChoiceScreen("Nadaj uprawnienia", GRANT_TYPES), self._grant_type)

    def _grant_type(self, kind: str | None) -> None:
        if kind is None:
            return
        forms = {
            "person": self._person_form,
            "entity": self._entity_form,
            "authorization": self._authorization_form,
            "indirect": self._indirect_form,
            "subunit": self._subunit_form,
            "eu_admin": self._eu_admin_form,
            "eu_representative": self._eu_representative_form,
        }
        screen, handler = forms[kind]()
        # Błąd KSeF (np. brak uprawnień, zły identyfikator) zostawia formularz otwarty z wpisanymi danymi.
        screen.on_submit = self._grant(handler)
        self.app.push_screen(screen)

    def _grant(
        self, handler: Callable[[KsefClient, dict[str, Any]], Awaitable[str]]
    ) -> Callable[[dict[str, Any]], Awaitable[str | None]]:
        async def grant(client: KsefClient, values: dict[str, Any]) -> Any:
            return await client.wait_for_permission_operation(await handler(client, values))

        return self.form_operation(
            lambda v: f"Nadanie uprawnień: {_recipient(v)}", grant, success="Uprawnienia nadane."
        )

    @property
    def _company(self) -> str:
        profile = self.app.profile
        context = fmt_ident({"type": profile.context_type, "value": profile.context_id})
        return f"{escape(profile.seller.nazwa)} ({escape(context)})"

    def _person_form(self):
        form = FormScreen(
            "Uprawnienia dla osoby fizycznej",
            _person_fields(PERSON_PERMISSIONS),
            intro="Osoba zaloguje się do KSeF własnym podpisem, profilem zaufanym albo certyfikatem.",
            submit="Nadaj",
            validate=_validate_person,
            wide=True,
            review=lambda v: _review(
                [
                    ("Kto", escape(_person(v))),
                    ("Uprawnienia", escape(_permissions(v["permissions"]))),
                    ("W imieniu", self._company),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            return await client.grant_person_permissions(
                v["kind"], v["value"], v["permissions"], v["description"], _subject_details(v)
            )

        return form, handler

    def _entity_form(self):
        options = [(PERMISSION_LABELS[p], p) for p in ENTITY_PERMISSIONS]
        form = FormScreen(
            "Uprawnienia dla innej firmy",
            [
                FormField("nip", "NIP firmy", required=True, check=check_nip, max_length=10, restrict=r"[0-9]*"),
                FormField(
                    "name",
                    "Pełna nazwa firmy",
                    required=True,
                    check=lambda v: None if 5 <= len(v) <= 90 else "od 5 do 90 znaków",
                ),
                FormField("permissions", "Uprawnienia", "multi", ["InvoiceRead"], options),
                FormField("delegate", "Z prawem dalszego przekazywania (np. pracownikom biura)", "checkbox", False),
                FormField("description", "Opis (widoczny w KSeF)", required=True, check=check_description),
            ],
            submit="Nadaj",
            validate=lambda v: None if v["permissions"] else ("permissions", "wybierz co najmniej jedno uprawnienie"),
            review=lambda v: _review(
                [
                    ("Kto", escape(f"{v['name']} (NIP {v['nip']})")),
                    ("Uprawnienia", escape(_permissions(v["permissions"]))),
                    ("W imieniu", self._company),
                    (
                        "Przekazywanie dalej",
                        "[$text-warning]tak[/] - firma może przekazać je swoim pracownikom" if v["delegate"] else "nie",
                    ),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            permissions = [(p, bool(v["delegate"])) for p in v["permissions"]]
            return await client.grant_entity_permissions(v["nip"], permissions, v["description"], v["name"])

        return form, handler

    def _authorization_form(self):
        options = [(PERMISSION_LABELS[p], p) for p in AUTHORIZATION_PERMISSIONS]
        form = FormScreen(
            "Uprawnienie podmiotowe",
            [
                FormField(
                    "kind", "Identyfikator", "select", "Nip", [("NIP", "Nip"), ("Peppol ID", "PeppolId")], required=True
                ),
                FormField("value", "NIP albo Peppol ID", required=True),
                FormField("name", "Pełna nazwa podmiotu", required=True),
                FormField("permission", "Uprawnienie", "select", "SelfInvoicing", options, required=True),
                FormField("description", "Opis (widoczny w KSeF)", required=True, check=check_description),
            ],
            submit="Nadaj",
            validate=lambda v: ("value", p) if (p := check_identifier(v["kind"])(v["value"])) else None,
            review=lambda v: _review(
                [
                    ("Kto", escape(f"{v['name']} ({IDENTIFIER_TYPES.get(v['kind'], v['kind'])} {v['value']})")),
                    ("Uprawnienie", escape(PERMISSION_LABELS.get(v["permission"], v["permission"]))),
                    ("W imieniu", self._company),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            return await client.grant_authorization(v["kind"], v["value"], v["permission"], v["description"], v["name"])

        return form, handler

    def _indirect_form(self):
        fields = _person_fields(["InvoiceRead", "InvoiceWrite", "CollectiveIdentifierManage"])
        fields[-1:-1] = [
            FormField(
                "target",
                "Klient",
                "select",
                "Nip",
                [("NIP klienta", "Nip"), ("identyfikator wewnętrzny klienta", "InternalId")],
                required=True,
                show_if=("all_clients", [False]),
            ),
            FormField(
                "target_value",
                "NIP / identyfikator klienta",
                placeholder="np. 1234567890 albo 1234567890-00011",
                show_if=("target", ["Nip", "InternalId"]),
            ),
            FormField(
                "all_clients",
                "Dla wszystkich klientów biura (zamiast jednego)",
                "checkbox",
                False,
                hint="Pracownik dostanie dostęp do faktur każdego klienta, który przekazał biuru uprawnienia "
                "z prawem dalszego przekazywania - także klientów, którzy dołączą później.",
            ),
        ]

        def validate(v: dict[str, Any]) -> tuple[str, str] | None:
            if problem := _validate_person(v):
                return problem
            if v["all_clients"]:
                return None
            if not v.get("target_value"):
                return "target_value", "wskaż klienta albo zaznacz „Dla wszystkich klientów biura”"
            if problem := check_unit(v["target"])(v["target_value"]):
                return "target_value", problem
            return None

        def scope(v: dict[str, Any]) -> str:
            if v["all_clients"]:
                return "[$text-warning]wszystkich klientów biura[/] (także przyszłych)"
            return escape(f"{IDENTIFIER_TYPES.get(v['target'], v['target'])} {v['target_value']}")

        form = FormScreen(
            "Uprawnienia pośrednie",
            fields,
            intro="Przekazanie pracownikowi uprawnień, które Twoja firma otrzymała od klientów "
            "(z prawem dalszego przekazywania).",
            submit="Nadaj",
            validate=validate,
            wide=True,
            review=lambda v: _review(
                [
                    ("Kto", escape(_person(v))),
                    ("Uprawnienia", escape(_permissions(v["permissions"]))),
                    ("Faktury klienta", scope(v)),
                    ("Przekazuje", self._company),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            target = ("AllPartners", None) if v["all_clients"] else (v["target"], v["target_value"])
            return await client.grant_indirect_permissions(
                v["kind"], v["value"], v["permissions"], v["description"], _subject_details(v), target
            )

        return form, handler

    def _subunit_form(self):
        fields = _person_fields()
        fields[-1:-1] = [
            FormField(
                "context",
                "Rodzaj jednostki",
                "select",
                "InternalId",
                [("identyfikator wewnętrzny", "InternalId"), ("NIP jednostki", "Nip")],
                required=True,
            ),
            FormField(
                "context_value",
                "Identyfikator jednostki",
                required=True,
                placeholder="NIP-5 cyfr, np. 1234567890-00011",
            ),
            FormField("subunit", "Nazwa jednostki (dla nowego identyfikatora)"),
        ]

        def validate(v: dict[str, Any]) -> tuple[str, str] | None:
            if problem := _validate_person(v):
                return problem
            if problem := check_unit(v["context"])(v["context_value"]):
                return "context_value", problem
            return None

        def unit(v: dict[str, Any]) -> str:
            text = f"{IDENTIFIER_TYPES.get(v['context'], v['context'])} {v['context_value']}"
            return escape(text + (f" „{v['subunit']}”" if v.get("subunit") else ""))

        form = FormScreen(
            "Administrator jednostki podrzędnej",
            fields,
            submit="Nadaj",
            validate=validate,
            wide=True,
            review=lambda v: _review(
                [
                    ("Kto", escape(_person(v))),
                    ("Rola", "administrator jednostki podrzędnej"),
                    ("Jednostka", unit(v)),
                    ("W imieniu", self._company),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            return await client.grant_subunit_admin(
                v["kind"],
                v["value"],
                (v["context"], v["context_value"]),
                v["description"],
                _subject_details(v),
                v.get("subunit"),
            )

        return form, handler

    def _eu_fields(self, *, admin: bool) -> list[FormField]:
        fields = [
            FormField(
                "fingerprint",
                "Odcisk certyfikatu uprawnionego (SHA-256)",
                required=True,
                check=check_identifier("Fingerprint"),
            ),
            FormField(
                "subject",
                "Uprawniony to",
                "select",
                "person",
                [("osoba", "person"), ("podmiot", "entity")],
                required=True,
            ),
            FormField("first", "Imię", show_if=("subject", ["person"])),
            FormField("last", "Nazwisko", show_if=("subject", ["person"])),
            FormField(
                "person_id_type",
                "PESEL lub NIP osoby",
                "select",
                "Pesel",
                [("PESEL", "Pesel"), ("NIP", "Nip")],
                show_if=("subject", ["person"]),
            ),
            FormField("person_id", "Numer", show_if=("subject", ["person"])),
            FormField("entity_name", "Nazwa podmiotu", show_if=("subject", ["entity"])),
            FormField("entity_address", "Adres podmiotu", show_if=("subject", ["entity"])),
        ]
        if admin:
            fields += [
                FormField(
                    "context",
                    "NIP-VAT UE (kontekst)",
                    required=True,
                    placeholder=f"{self.app.profile.seller.nip}-DE123456789",
                ),
                FormField("eu_name", "Nazwa podmiotu z UE", required=True),
                FormField("eu_address", "Adres podmiotu z UE", required=True),
            ]
        else:
            options = [(PERMISSION_LABELS[p], p) for p in ("InvoiceWrite", "InvoiceRead")]
            fields.append(FormField("permissions", "Uprawnienia", "multi", ["InvoiceRead"], options))
        fields.append(FormField("description", "Opis (widoczny w KSeF)", required=True, check=check_description))
        return fields

    @staticmethod
    def _eu_subject(v: dict[str, Any]) -> dict[str, Any]:
        if v["subject"] == "entity":
            return {
                "subjectDetailsType": "EntityByFingerprint",
                "entityByFp": {"fullName": v["entity_name"], "address": v["entity_address"]},
            }
        identifier = {"type": v["person_id_type"], "value": v["person_id"]}
        return {
            "subjectDetailsType": "PersonByFingerprintWithIdentifier",
            "personByFpWithId": {"firstName": v["first"], "lastName": v["last"], "identifier": identifier},
        }

    @staticmethod
    def _validate_eu(v: dict[str, Any]) -> tuple[str, str] | None:
        keys = ("first", "last", "person_id") if v["subject"] == "person" else ("entity_name", "entity_address")
        for key in keys:
            if not v.get(key):
                return key, "to pole jest wymagane"
        if v["subject"] == "person" and (problem := check_identifier(v["person_id_type"])(v["person_id"])):
            return "person_id", problem
        if v.get("context") and (problem := check_nip_vat_ue(v["context"])):
            return "context", problem
        if "permissions" in v and not v["permissions"]:
            return "permissions", "wybierz co najmniej jedno uprawnienie"
        return None

    @staticmethod
    def _eu_who(v: dict[str, Any]) -> str:
        who = f"{v['first']} {v['last']}" if v["subject"] == "person" else v["entity_name"]
        return escape(f"{who} (certyfikat {v['fingerprint'][:16]}…)")

    def _eu_admin_form(self):
        form = FormScreen(
            "Administrator podmiotu unijnego",
            self._eu_fields(admin=True),
            submit="Nadaj",
            validate=self._validate_eu,
            wide=True,
            review=lambda v: _review(
                [
                    ("Kto", self._eu_who(v)),
                    ("Rola", "administrator podmiotu unijnego"),
                    ("Kontekst", escape(f"NIP-VAT UE {v['context'].upper()}")),
                    ("Podmiot z UE", escape(f"{v['eu_name']}, {v['eu_address']}")),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            details = {"fullName": v["eu_name"], "address": v["eu_address"]}
            return await client.grant_eu_entity_admin(
                v["fingerprint"].upper(),
                v["context"],
                v["description"],
                f"{v['eu_name']}, {v['eu_address']}",
                self._eu_subject(v),
                details,
            )

        return form, handler

    def _eu_representative_form(self):
        form = FormScreen(
            "Reprezentant podmiotu unijnego",
            self._eu_fields(admin=False),
            submit="Nadaj",
            validate=self._validate_eu,
            wide=True,
            review=lambda v: _review(
                [
                    ("Kto", self._eu_who(v)),
                    ("Uprawnienia", escape(_permissions(v["permissions"]))),
                    ("W imieniu", self._company),
                ]
            ),
        )

        async def handler(client: KsefClient, v: dict[str, Any]) -> str:
            return await client.grant_eu_entity_representative(
                v["fingerprint"].upper(), v["permissions"], v["description"], self._eu_subject(v)
            )

        return form, handler
