"""The lock screen shown before anything else.

Two modes, chosen automatically: **create** on first run (no credential stored)
and **unlock** afterwards. `app.auth` does the hashing; this file is only the
form around it and never holds the password beyond the widget.

The card leads with the app logo (`theme.app_logo`), not the academy logo from
`BrandConfig` -- that one is for the PowerPoint title slide.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import auth
from app.ui import theme

_LOG = logging.getLogger(__name__)

#: Wrong attempts before the field is briefly disabled, then the step-up in
#: seconds per further failure. Enough to stop guessing by hand; not enough to
#: punish a typo.
_LOCK_AFTER = 3
_LOCK_STEP_S = 5

_TITLE = "KRISHNA'S BATCOMPUTER"

#: Height of the logo on the card. Big enough to actually read the image, small
#: enough that the whole card still fits a 768 px-tall laptop screen without
#: scrolling (logo + title + field + button lands around 640 px).
_LOGO_PX = 210


class LoginScreen(QWidget):
    """Asks for the password, or sets one up the first time.

    Emits `unlocked` once, when the password is accepted or freshly created.
    """

    unlocked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("screen", True)
        self._failures = 0
        self._lock_timer = QTimer(self)
        self._lock_timer.setSingleShot(True)
        self._lock_timer.timeout.connect(self._unlock_input)
        self._creating = not auth.is_password_set()

        self._build()
        self.refresh()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)

        card = QFrame(self)
        card.setObjectName("loginCard")
        card.setProperty("card", True)
        card.setMinimumWidth(430)
        card.setMaximumWidth(470)
        #: Kept so `_shrink_to_fit` can re-run the layout after a mode change.
        self._card = card
        column = QVBoxLayout(card)
        column.setContentsMargins(26, 22, 26, 20)
        column.setSpacing(8)

        # The app's logo is the face of the lock screen; the padlock glyph is the
        # fallback for a build where the asset is missing, so the card never
        # opens with an empty hole at the top.
        self._logo = QLabel(card)
        self._logo.setObjectName("loginLogo")
        self._logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        logo = theme.app_logo(_LOGO_PX)
        if logo.isNull():
            self._logo.setObjectName("cardGlyph")
            self._logo.setText("🔒")
        else:
            self._logo.setPixmap(logo)
        column.addWidget(self._logo)

        self._title = QLabel(_TITLE, card)
        self._title.setObjectName("h1")
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self._title)

        self._subtitle = QLabel("", card)
        self._subtitle.setObjectName("subtitle")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._subtitle.setWordWrap(True)
        column.addWidget(self._subtitle)
        column.addSpacing(8)

        self._password = QLineEdit(card)
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Password")
        self._password.returnPressed.connect(self._submit)
        self._password.textChanged.connect(self._on_typing)

        show = QPushButton("Show", card)
        show.setCheckable(True)
        # Wide enough that the label is not clipped by the button's own padding.
        show.setMinimumWidth(76)
        show.toggled.connect(self._on_show_toggled)

        pw_row = QHBoxLayout()
        pw_row.setSpacing(6)
        pw_row.addWidget(self._password, 1)
        pw_row.addWidget(show, 0)
        column.addLayout(pw_row)

        self._confirm = QLineEdit(card)
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._confirm.setPlaceholderText("Confirm password")
        self._confirm.returnPressed.connect(self._submit)
        self._confirm.textChanged.connect(self._on_typing)
        column.addWidget(self._confirm)

        self._notice = QLabel("", card)
        self._notice.setObjectName("notice")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        column.addWidget(self._notice)

        self._button = QPushButton("Unlock", card)
        theme.set_variant(self._button, "primary")
        self._button.setDefault(True)
        self._button.clicked.connect(self._submit)
        column.addWidget(self._button)

        self._hint = QLabel("", card)
        self._hint.setObjectName("cardDesc")
        self._hint.setWordWrap(True)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self._hint)

        row.addWidget(card, 0)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)

    # -- mode --------------------------------------------------------------- #

    def refresh(self) -> None:
        """Re-read whether a credential exists and lay the form out for it.

        Called every time the screen is shown, including on re-lock, so it has to
        put the form back into a *usable* state and not merely an empty one.
        """
        self._creating = not auth.is_password_set()
        self._failures = 0
        self._enable_input()
        self._password.clear()
        self._confirm.clear()
        self._set_notice("")

        if self._creating:
            self._subtitle.setText(
                "Set a password to lock the app. You will be asked for it each "
                "time it opens."
            )
            self._confirm.setVisible(True)
            self._button.setText("Set password")
            self._hint.setText(
                f"At least {auth.MIN_PASSWORD_LENGTH} characters. It is stored as "
                "a salted scrypt hash — it cannot be recovered, only replaced."
            )
            self._hint.setVisible(True)
        else:
            self._subtitle.setText("Enter your password to continue.")
            self._confirm.setVisible(False)
            self._button.setText("Unlock")
            # No recovery hint on the lock screen: the reset route is not
            # something to advertise to whoever is sitting in front of it.
            self._hint.clear()
            self._hint.setVisible(False)
        self._shrink_to_fit()
        self._password.setFocus()

    def _shrink_to_fit(self) -> None:
        """Re-measure the card after its wrapped labels changed length.

        A word-wrapped `QLabel` keeps the height it was given: switching from the
        two-line "Set a password…" text to the one-line "Enter your password…"
        leaves a 55 px hole under it until the layout is invalidated. Only
        shrinking needs this -- growing happens on its own.
        """
        for label in (self._subtitle, self._hint, self._notice):
            label.updateGeometry()
        layout = self._card.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    # -- events ------------------------------------------------------------- #

    def _on_show_toggled(self, shown: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        self._password.setEchoMode(mode)
        self._confirm.setEchoMode(mode)

    def _on_typing(self, _text: str) -> None:
        if self._notice.isVisible():
            self._set_notice("")

    def _set_notice(self, text: str, level: str = "error") -> None:
        self._notice.setText(text)
        self._notice.setProperty("level", level)
        self._notice.setVisible(bool(text))
        theme.repolish(self._notice)

    def _enable_input(self) -> None:
        """Undo whatever disabled the form, and cancel a pending throttle.

        Two things switch the controls off: the throttle after repeated failures,
        and the brief "Checking…" step while scrypt runs. Either can still be in
        force the next time this screen appears -- a successful unlock leaves the
        button disabled, and locking the app then showed a lock screen whose
        Unlock button was dead, so the correct password did nothing at all.
        `_submit` returns early when the button is disabled, which is right while
        checking and wrong forever after.
        """
        self._lock_timer.stop()
        self._password.setEnabled(True)
        self._button.setEnabled(True)

    def _unlock_input(self) -> None:
        self._enable_input()
        self._password.setFocus()

    def _throttle(self) -> None:
        """Disable the form briefly, longer with each further failure."""
        wait = _LOCK_STEP_S * (self._failures - _LOCK_AFTER + 1)
        self._password.setEnabled(False)
        self._button.setEnabled(False)
        self._set_notice(
            f"Too many attempts. Try again in {wait} seconds."
        )
        self._lock_timer.start(wait * 1000)

    def _submit(self) -> None:
        if not self._button.isEnabled():
            return
        password = self._password.text()

        if self._creating:
            if password != self._confirm.text():
                self._set_notice("The two passwords do not match.")
                self._confirm.setFocus()
                self._confirm.selectAll()
                return
            try:
                auth.set_password(password)
            except auth.AuthError as exc:
                self._set_notice(str(exc))
                self._password.setFocus()
                return
            _LOG.info("password created")
            self._password.clear()
            self._confirm.clear()
            self.unlocked.emit()
            return

        if not password:
            self._set_notice("Enter your password.")
            return
        # Hashing takes a moment; say so rather than looking frozen.
        self._button.setEnabled(False)
        self._set_notice("Checking…", "info")
        QTimer.singleShot(0, lambda: self._check(password))

    def _check(self, password: str) -> None:
        ok = False
        try:
            ok = auth.verify_password(password)
        except Exception as exc:  # noqa: BLE001 - never strand the user here
            _LOG.error("password check failed: %s", exc)
            self._set_notice("The password could not be checked. See the log.")
            self._button.setEnabled(True)
            return

        if ok:
            self._failures = 0
            # Before the emit, not after: re-locking shows this same screen
            # again, and it must not come back with a dead Unlock button.
            self._enable_input()
            self._password.clear()
            self._set_notice("")
            self.unlocked.emit()
            return

        self._button.setEnabled(True)
        self._failures += 1
        self._password.selectAll()
        self._password.setFocus()
        if self._failures >= _LOCK_AFTER:
            self._throttle()
        else:
            left = _LOCK_AFTER - self._failures
            self._set_notice(
                f"That password is not right. {left} attempt"
                f"{'s' if left != 1 else ''} before a short wait."
            )
