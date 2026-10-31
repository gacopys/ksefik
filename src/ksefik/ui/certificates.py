"""Certyfikaty KSeF: limity, lista, wniosek o nowy certyfikat (klucz EC P-256), pobieranie i unieważnianie."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.widgets import Static

from ..client import ConnectionInterrupted, EnrollmentRejected, KsefClient, KsefError
from ..codes import CERTIFICATE_STATUSES, CERTIFICATE_TYPES, REVOCATION_REASONS
from ..config import CONFIG_DIR
from ..xades import enrollment_csr, save_certificate, save_key
from .common import ConfirmScreen, FormField, FormScreen, ListScreen, fmt_dt, fmt_ident, short_path

if TYPE_CHECKING:
    from .app import KsefApp

CERT_DIR = CONFIG_DIR / "certyfikaty"
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\- ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{5,100}$")


class CertificatesScreen(ListScreen):
    TITLE_TEXT = "Certyfikaty KSeF"
    DESCRIPTION = (
        "Certyfikat KSeF służy do logowania (zamiast podpisu kwalifikowanego) albo do faktur w trybie offline. "
        "n - nowy, d - zapisz certyfikat, Del - unieważnij."
    )
    COLUMNS = [
        ("Nazwa", 26),
        ("Typ", 16),
        ("Status", 12),
        ("Ważny do", 16),
        ("Numer seryjny", 18),
        ("Ostatnio użyty", 16),
    ]
    EMPTY = "Nie masz jeszcze certyfikatów KSeF. Naciśnij n, aby złożyć wniosek."
    REQUIRES = (
        "Certyfikaty KSeF są dostępne po zalogowaniu podpisem (XAdES) - kwalifikowanym podpisem, pieczęcią, "
        "profilem zaufanym albo innym certyfikatem KSeF. Logowanie tokenem nie wystarcza."
    )
    BINDINGS = [
        *ListScreen.BINDINGS,
        Binding("n", "enroll", "Nowy certyfikat"),
        Binding("d", "download", "Zapisz certyfikat"),
        Binding("p", "resume", "Ponów pobranie"),
        Binding("delete", "revoke", "Unieważnij", key_display="Del"),
    ]

    app: KsefApp

    def compose_header(self) -> ComposeResult:
        yield Static("", id="cert-limits", classes="screen-description")
        yield Static("", id="cert-pending", classes="screen-description")

    def on_mount(self) -> None:
        self._show_pending()
        if self.pending_enrollments():
            self.action_resume()  # wnioski z poprzedniego uruchomienia - certyfikat mógł już powstać

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "resume":
            return bool(self.is_mounted and self.pending_enrollments())
        return super().check_action(action, parameters)

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        limits = await client.certificate_limits()
        enrollment, certificate = limits.get("enrollment", {}), limits.get("certificate", {})
        can = (
            "[$text-success]możesz złożyć wniosek[/]"
            if limits.get("canRequest")
            else "[$text-warning]limit wniosków wyczerpany[/]"
        )
        certs = f"{certificate.get('remaining', '?')} z {certificate.get('limit', '?')}"
        enrollments = f"{enrollment.get('remaining', '?')} z {enrollment.get('limit', '?')}"
        self.query_one("#cert-limits", Static).update(
            f"{can}   [$foreground-muted]certyfikaty:[/] {certs} dostępnych   "
            f"[$foreground-muted]wnioski:[/] {enrollments}"
        )
        return await client.query_certificates()

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        status = item.get("status", "")
        style = {"Active": "green", "Blocked": "yellow", "Revoked": "red", "Expired": "dim"}.get(status, "")
        return (
            item.get("name", ""),
            CERTIFICATE_TYPES.get(item.get("type", ""), item.get("type", "")),
            Text(CERTIFICATE_STATUSES.get(status, status), style=style),
            fmt_dt(item.get("validTo")),
            item.get("certificateSerialNumber", ""),
            fmt_dt(item.get("lastUseDate")) or "—",
        )

    def details(self, item: dict[str, Any]) -> str:
        subject = escape(fmt_ident(item.get("subjectIdentifier")))
        return (
            f"[$foreground-muted]CN:[/] {escape(item.get('commonName', ''))}   "
            f"[$foreground-muted]podmiot:[/] {subject}   "
            f"[$foreground-muted]ważny od:[/] {fmt_dt(item.get('validFrom'))}"
        )

    # ------------------------------------------------------------------ wniosek

    def action_enroll(self) -> None:
        form = FormScreen(
            "Wniosek o certyfikat KSeF",
            [
                FormField(
                    "name",
                    "Nazwa certyfikatu",
                    value="ksefik",
                    required=True,
                    check=lambda v: None if NAME_PATTERN.match(v) else "5-100 znaków: litery, cyfry, spacje, - i _",
                ),
                FormField(
                    "type",
                    "Przeznaczenie",
                    "select",
                    "Authentication",
                    [("logowanie do KSeF", "Authentication"), ("faktury w trybie offline (kod QR II)", "Offline")],
                    required=True,
                ),
                FormField(
                    "password", "Hasło do klucza prywatnego", "password", hint="zalecane; puste = klucz bez hasła"
                ),
                FormField("valid_from", "Ważny od (opcjonalnie)", "date"),
            ],
            intro=(
                "Aplikacja wygeneruje nowy klucz EC P-256 i wniosek (CSR) z danymi z Twojego certyfikatu logowania. "
                f"Klucz i certyfikat zapiszę w [b]{escape(short_path(CERT_DIR))}[/b]."
            ),
            submit="Złóż wniosek",
        )
        self.app.push_screen(form, lambda v: v and self._enroll(v))

    @work(exclusive=True, group="op")
    async def _enroll(self, values: dict[str, Any]) -> None:
        client = await self.app.get_client()
        if client is None:
            return
        name = values["name"]
        slug = re.sub(r"[^\w-]+", "_", name)
        base = CERT_DIR / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"
        key_path = base.with_suffix(".klucz.pem")
        self.show_outcome("[$text-accent]⟳ Składam wniosek o certyfikat…[/]")
        try:
            data = await client.enrollment_data()
        except KsefError as exc:
            self._failed(exc)
            return
        csr, key = enrollment_csr(data)
        save_key(key, key_path, values.get("password"))  # klucz zapisany przed wysłaniem wniosku
        valid_from = values.get("valid_from")
        start = datetime.combine(valid_from, time.min, tzinfo=UTC) if valid_from else None
        try:
            enrollment = await client.enroll_certificate(name, values["type"], csr, start)
        except ConnectionInterrupted as exc:
            # Wniosek mógł dotrzeć do KSeF - klucz zostaje, certyfikat pojawi się na liście.
            note = f"Nie wiadomo, czy KSeF przyjął wniosek. Klucz prywatny zachowano: {short_path(key_path)}"
            self._failed(exc, note)
            return
        except KsefError as exc:
            key_path.unlink(missing_ok=True)  # wniosku nie złożono - klucz nie jest do niczego potrzebny
            self._failed(exc)
            return
        profile = self.app.profile
        pending = {
            "reference": enrollment["referenceNumber"],
            "name": name,
            "type": values["type"],
            "env": profile.env,
            "context": profile.context_id,
            "key": str(key_path),
            "certificate": str(base.with_suffix(".pem")),
            "created": datetime.now(UTC).isoformat(),
        }
        sidecar = base.with_suffix(".wniosek.json")
        sidecar.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        await self._finish(client, sidecar, pending)

    def _failed(self, exc: KsefError, note: str = "") -> None:
        hint = f"\n\n{self.REQUIRES}" if exc.http_status in (400, 403) else ""
        if note:
            hint += f"\n\n{note}"
        self.show_outcome(f"[$text-error]Wniosek o certyfikat: {escape(exc.message)}[/]")
        self.notify(exc.human() + hint, title="Wniosek o certyfikat", severity="error", timeout=15)

    def pending_enrollments(self) -> list[tuple[Path, dict[str, Any]]]:
        """Wnioski złożone z tej aplikacji, których certyfikatu jeszcze nie pobrano (dla bieżącej firmy)."""
        profile = self.app.profile
        found = []
        for path in sorted(CERT_DIR.glob("*.wniosek.json")) if CERT_DIR.is_dir() else []:
            try:
                pending = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if pending.get("env") == profile.env and pending.get("context") == profile.context_id:
                found.append((path, pending))
        return found

    def _show_pending(self) -> None:
        pending = self.pending_enrollments()
        text = "\n".join(
            f"[$text-warning]Wniosek „{escape(p['name'])}” ({escape(p['reference'])}) czeka na pobranie "
            "certyfikatu - p, aby ponowić.[/]"
            for _, p in pending
        )
        widget = self.query_one("#cert-pending", Static)
        widget.update(text)
        widget.display = bool(text)
        self.refresh_bindings()

    async def _finish(self, client: KsefClient, sidecar: Path, pending: dict[str, Any]) -> None:
        """Czeka na certyfikat dla złożonego wniosku i zapisuje go obok klucza. Przy błędzie wniosek zostaje."""
        reference = pending["reference"]
        key_path, cert_path = Path(pending["key"]), Path(pending["certificate"])
        try:
            status = await client.wait_for_enrollment(reference)
            serial = status.get("certificateSerialNumber")
            if not serial:
                self.show_outcome(
                    f"[$text-warning]Wniosek {escape(reference)} przyjęty, certyfikat jeszcze się generuje. "
                    "Pobierzesz go później - p (Ponów pobranie).[/]"
                )
                self._show_pending()
                return
            certificate = (await client.retrieve_certificates([serial]))[0]
        except EnrollmentRejected as exc:
            sidecar.unlink(missing_ok=True)
            key_path.unlink(missing_ok=True)
            self._show_pending()
            self._failed(exc)
            return
        except KsefError as exc:
            self.show_outcome(
                f"[$text-warning]Klucz jest zapisany. Nie udało się pobrać certyfikatu dla wniosku "
                f"{escape(reference)}: {escape(exc.message)}. Naciśnij p, aby ponowić.[/]"
            )
            self._show_pending()
            return
        save_certificate(certificate["certificate"], cert_path)
        sidecar.unlink(missing_ok=True)
        self._show_pending()
        hint = ""
        if pending["type"] == "Authentication":
            snippet = "\n".join(
                [
                    "[logowanie]",
                    'metoda = "certyfikat"',
                    f'certyfikat = "{short_path(cert_path)}"',
                    f'klucz = "{short_path(key_path)}"',
                ]
            )
            self.app.copy_to_clipboard(snippet)
            copied = " (skopiowano do schowka)" if self.app.clipboard_confirmed else ""
            hint = f"\n\nAby logować się tym certyfikatem, wklej do profilu{copied}:\n" + snippet
        self.show_outcome(f"[$text-success]✓ Certyfikat {escape(serial)} gotowy: {escape(short_path(cert_path))}[/]")
        self.notify(
            f"Certyfikat {serial} gotowy.\nCertyfikat: {short_path(cert_path)}\nKlucz: {short_path(key_path)}{hint}",
            title="Certyfikat KSeF",
            timeout=20,
        )
        self.action_reload()

    @work(exclusive=True, group="op")
    async def action_resume(self) -> None:
        """Ponawia pobranie certyfikatów dla wniosków, które czekają (także z poprzednich uruchomień)."""
        pending = self.pending_enrollments()
        if not pending:
            return
        client = await self.app.get_client()
        if client is None:
            return
        for sidecar, data in pending:
            self.show_outcome(f"[$text-accent]⟳ Pobieram certyfikat dla wniosku {escape(data['reference'])}…[/]")
            await self._finish(client, sidecar, data)

    # ------------------------------------------------------------- pobieranie

    @work(exclusive=True, group="op")
    async def action_download(self) -> None:
        item = self.selected
        if item is None:
            return
        client = await self.app.get_client()
        if client is None:
            return
        try:
            certificate = (await client.retrieve_certificates([item["certificateSerialNumber"]]))[0]
        except KsefError as exc:
            self.notify(exc.human(), title="Certyfikat", severity="error")
            return
        path = CERT_DIR / f"{item['certificateSerialNumber']}.pem"
        save_certificate(certificate["certificate"], path)
        self.notify(f"Zapisano certyfikat:\n{short_path(path)}", title="Certyfikat", timeout=6)

    # ----------------------------------------------------------- unieważnianie

    def action_revoke(self) -> None:
        item = self.selected
        if item is None or item.get("status") not in ("Active", "Blocked"):
            return
        form = FormScreen(
            "Unieważnienie certyfikatu",
            [
                FormField(
                    "reason",
                    "Powód",
                    "select",
                    "Unspecified",
                    [(label, key) for key, label in REVOCATION_REASONS.items()],
                    required=True,
                )
            ],
            intro=f"[b]{escape(item.get('name', ''))}[/b] ({item['certificateSerialNumber']})\n"
            "Unieważnionego certyfikatu nie można przywrócić.",
            submit="Unieważnij",
        )

        def chosen(values: dict[str, Any] | None) -> None:
            if values:
                self.app.push_screen(
                    ConfirmScreen("Na pewno?", "Certyfikat przestanie działać natychmiast.", confirm="Unieważnij"),
                    lambda ok: ok and self._revoke(item["certificateSerialNumber"], values["reason"]),
                )

        self.app.push_screen(form, chosen)

    @work(exclusive=True, group="op")
    async def _revoke(self, serial: str, reason: str) -> None:
        await self.run_operation(
            "Unieważnienie certyfikatu",
            lambda c: c.revoke_certificate(serial, reason),
            success="Certyfikat unieważniony.",
        )
