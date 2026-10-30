"""
Tests for the fork's safety features: protected senders, history + undo,
previews, the Cloudflare Access gate and the pasted OAuth redirect.
"""

import datetime as dt
import json
import threading
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt
from google.auth import jwt as google_jwt
from googleapiclient.errors import HttpError

from app.core import settings, state
from app.services import history, protected
from app.services.gmail import safety

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "access_team_domain", "")
    monkeypatch.setattr(settings, "access_aud", "")
    monkeypatch.setattr(settings, "access_allowed_emails", "")
    return tmp_path


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeBatch:
    def __init__(self, callback):
        self.callback = callback
        self.requests = []

    def add(self, request, request_id=None):
        self.requests.append((request, request_id))

    def execute(self):
        for i, (req, rid) in enumerate(self.requests):
            try:
                self.callback(rid or str(i), req.execute(), None)
            except Exception as e:  # noqa: BLE001
                self.callback(rid or str(i), None, e)


class FakeGmail:
    """Tiny in-memory Gmail: messages have id, sender, subject, labels."""

    def __init__(self, messages, fail_trash_batch_modify=False):
        self.msgs = {m["id"]: {**m, "labels": set(m.get("labels", ["INBOX"]))} for m in messages}
        self.modify_calls = []
        self.fail_trash_batch_modify = fail_trash_batch_modify

    # --- query matching (enough for from:/-from:/is:unread/in:inbox)
    def _match(self, msg, q):
        for term in (q or "").split():
            neg = term.startswith("-")
            t = term[1:] if neg else term
            if t.startswith("from:"):
                ok = t[5:] in msg["sender"]
            elif t == "is:unread":
                ok = "UNREAD" in msg["labels"]
            elif t == "in:inbox":
                ok = "INBOX" in msg["labels"]
            else:
                ok = True
            if ok == neg:
                return False
        return "TRASH" not in msg["labels"]

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q=None, maxResults=500, pageToken=None):
        ids = [m for m in self.msgs if self._match(self.msgs[m], q)]
        start = int(pageToken or 0)
        page = ids[start : start + maxResults]
        out = {"messages": [{"id": i} for i in page]}
        if start + maxResults < len(ids):
            out["nextPageToken"] = str(start + maxResults)
        return _Exec(lambda: out)

    def get(self, userId, id, format=None, metadataHeaders=None):
        def run():
            m = self.msgs[id]
            return {
                "id": id,
                "payload": {
                    "headers": [
                        {"name": "From", "value": f"Someone <{m['sender']}>"},
                        {"name": "Subject", "value": m.get("subject", "hi")},
                        {"name": "Date", "value": "Fri, 30 Oct 2026 10:00:00 -0400"},
                    ]
                },
            }

        return _Exec(run)

    def batchModify(self, userId, body):
        def run():
            if self.fail_trash_batch_modify and "TRASH" in body.get("removeLabelIds", []):
                raise HttpError(type("R", (), {"status": 400, "reason": "bad"})(), b"no")
            self.modify_calls.append(body)
            for i in body["ids"]:
                self.msgs[i]["labels"] |= set(body.get("addLabelIds", []))
                self.msgs[i]["labels"] -= set(body.get("removeLabelIds", []))
            return {}

        return _Exec(run)

    def untrash(self, userId, id):
        def run():
            if id not in self.msgs:
                raise KeyError(id)
            self.msgs[id]["labels"].discard("TRASH")
            return {"id": id}

        return _Exec(run)

    def new_batch_http_request(self, callback):
        return FakeBatch(callback)


def make_inbox():
    return FakeGmail(
        [
            {"id": "a1", "sender": "deals@shop.com", "labels": ["INBOX", "UNREAD"]},
            {"id": "a2", "sender": "deals@shop.com", "labels": ["INBOX", "UNREAD"]},
            {"id": "b1", "sender": "registrar@njit.edu", "labels": ["INBOX", "UNREAD"]},
            {"id": "b2", "sender": "prof@cs.njit.edu", "labels": ["INBOX", "UNREAD"]},
            {"id": "c1", "sender": "news@shop.com", "labels": ["INBOX"]},
            {"id": "d1", "sender": "boss@shop.com", "labels": ["INBOX", "UNREAD"]},
        ]
    )


# ------------------------------------------------------- protected senders


class TestProtected:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("NJIT.edu", "njit.edu"),
            ("*@njit.edu", "njit.edu"),
            ("@njit.edu", "njit.edu"),
            ("*.njit.edu", "njit.edu"),
            ("  Me@Gmail.com ", "me@gmail.com"),
            ("njit", None),
            ("from:x.com OR is:starred", None),
            ("a@b", None),
            ("", None),
        ],
    )
    def test_normalize(self, raw, expected):
        assert protected.normalize_entry(raw) == expected

    def test_is_protected_domain_covers_subdomains(self):
        entries = ["njit.edu", "boss@shop.com"]
        assert protected.is_protected("registrar@njit.edu", entries)
        assert protected.is_protected("prof@cs.njit.edu", entries)
        assert protected.is_protected("njit.edu", entries)
        assert protected.is_protected("BOSS@shop.com", entries)
        assert not protected.is_protected("deals@shop.com", entries)
        assert not protected.is_protected("shop.com", entries)
        assert not protected.is_protected("evilnjit.edu", entries)

    def test_add_remove_persist(self, tmp_data_dir):
        protected.add("*@njit.edu")
        protected.add("boss@shop.com")
        protected.add("njit.edu")  # duplicate
        assert protected.load() == ["boss@shop.com", "njit.edu"]
        protected.remove("@njit.edu")
        assert protected.load() == ["boss@shop.com"]
        saved = json.loads((tmp_data_dir / "protected_senders.json").read_text())
        assert saved == {"senders": ["boss@shop.com"]}

    def test_add_rejects_garbage(self):
        with pytest.raises(ValueError):
            protected.add("not an email")

    def test_corrupted_file_fails_closed(self, tmp_data_dir):
        (tmp_data_dir / "protected_senders.json").write_text("{broken")
        with pytest.raises(protected.ProtectedListError):
            protected.load()

    def test_exclusion_query(self):
        assert protected.with_protection("from:shop.com", ["njit.edu", "boss@shop.com"]) == (
            "from:shop.com -from:njit.edu -from:boss@shop.com"
        )
        assert protected.with_protection("is:unread", []) == "is:unread"

    def test_filter_protected_ids_checks_real_sender(self):
        svc = make_inbox()
        keep, skipped = safety.filter_protected_ids(
            svc, ["a1", "b1", "b2", "d1"], ["njit.edu", "boss@shop.com"]
        )
        assert keep == ["a1"]
        assert skipped == 3

    def test_filter_protected_ids_no_entries_makes_no_calls(self):
        class Boom:
            def new_batch_http_request(self, **_):
                raise AssertionError("should not be called")

        assert safety.filter_protected_ids(Boom(), ["x"], []) == (["x"], 0)


# ------------------------------------------------- actions respect the list


class TestActionsRespectProtection:
    def test_single_delete_refuses_protected_sender(self):
        protected.add("njit.edu")
        from app.services.gmail import delete

        with patch.object(delete, "get_gmail_service") as gs:
            result = delete.delete_emails_by_sender("registrar@njit.edu")
        gs.assert_not_called()
        assert result["success"] is False
        assert result["protected"] is True

    def test_domain_delete_skips_protected_address_and_records_history(self):
        protected.add("boss@shop.com")
        svc = make_inbox()
        from app.services.gmail import delete

        with patch.object(delete, "get_gmail_service", return_value=(svc, None)):
            result = delete.delete_emails_by_sender("shop.com")

        assert result["success"] is True
        assert result["deleted"] == 3  # a1, a2, c1 but not boss d1
        assert "TRASH" not in svc.msgs["d1"]["labels"]
        entries = history.list_entries()
        assert entries[0]["action"] == "trash" and entries[0]["count"] == 3

    def test_bulk_delete_skips_protected_senders(self):
        protected.add("njit.edu")
        svc = make_inbox()
        from app.services.gmail import delete

        with patch.object(delete, "get_gmail_service", return_value=(svc, None)):
            delete.delete_emails_bulk_background(["deals@shop.com", "registrar@njit.edu"])

        status = state.delete_bulk_status
        assert status["done"] and not status["error"]
        assert status["skipped_senders"] == ["registrar@njit.edu"]
        assert status["deleted_count"] == 2
        assert "TRASH" not in svc.msgs["b1"]["labels"]
        assert status["history_id"]

    def test_bulk_delete_all_protected_does_nothing(self):
        protected.add("njit.edu")
        from app.services.gmail import delete

        with patch.object(delete, "get_gmail_service") as gs:
            delete.delete_emails_bulk_background(["registrar@njit.edu"])
        gs.assert_not_called()
        assert "protected" in state.delete_bulk_status["message"]

    def test_bulk_delete_corrupted_list_deletes_nothing(self, tmp_data_dir):
        (tmp_data_dir / "protected_senders.json").write_text("nope")
        from app.services.gmail import delete

        with patch.object(delete, "get_gmail_service") as gs:
            delete.delete_emails_bulk_background(["deals@shop.com"])
        gs.assert_not_called()
        assert "unreadable" in state.delete_bulk_status["error"]

    def test_archive_skips_protected(self):
        protected.add("njit.edu")
        svc = make_inbox()
        from app.services.gmail import archive

        with patch.object(archive, "get_gmail_service", return_value=(svc, None)):
            archive.archive_emails_background(["shop.com", "njit.edu"])

        assert state.archive_status["skipped_senders"] == ["njit.edu"]
        assert "INBOX" in svc.msgs["b1"]["labels"]
        assert "INBOX" not in svc.msgs["a1"]["labels"]
        assert state.archive_status["history_id"]

    def test_mark_read_excludes_protected(self):
        protected.add("njit.edu")
        svc = make_inbox()
        from app.services.gmail import mark_read

        with patch.object(mark_read, "get_gmail_service", return_value=(svc, None)):
            mark_read.mark_emails_as_read(count=0)

        assert state.mark_read_status["marked_count"] == 3  # a1 a2 d1
        assert "UNREAD" in svc.msgs["b1"]["labels"]
        assert "UNREAD" in svc.msgs["b2"]["labels"]

    def test_unsubscribe_refuses_protected(self):
        protected.add("njit.edu")
        from app.services.gmail.unsubscribe import unsubscribe_single

        result = unsubscribe_single("njit.edu", "https://njit.edu/unsub")
        assert result["success"] is False and result["protected"] is True


# ---------------------------------------------------------- history + undo


class TestHistory:
    def test_undo_trash_restores(self):
        svc = make_inbox()
        from app.services.gmail import delete

        with patch.object(delete, "get_gmail_service", return_value=(svc, None)):
            delete.delete_emails_by_sender("deals@shop.com")
        entry = history.list_entries()[0]
        assert "message_ids" not in entry and entry["can_undo"]

        result = history.undo(svc, entry["id"])
        assert result["restored"] == 2
        assert "TRASH" not in svc.msgs["a1"]["labels"]
        assert history.list_entries()[0]["undone"] is True
        assert history.undo(svc, entry["id"])["success"] is False

    def test_undo_trash_falls_back_to_untrash(self):
        svc = make_inbox()
        svc.fail_trash_batch_modify = True
        for i in ("a1", "a2"):
            svc.msgs[i]["labels"].add("TRASH")
        entry = history.record("trash", ["a1", "a2", "gone"], ["deals@shop.com"])
        result = history.undo(svc, entry["id"])
        assert result["restored"] == 2 and result["failed"] == 1
        assert "TRASH" not in svc.msgs["a2"]["labels"]

    def test_undo_archive_and_mark_read(self):
        svc = make_inbox()
        for i in ("a1", "a2"):
            svc.msgs[i]["labels"] -= {"INBOX", "UNREAD"}
        e1 = history.record("archive", ["a1"])
        e2 = history.record("mark_read", ["a2"])
        history.undo(svc, e1["id"])
        history.undo(svc, e2["id"])
        assert "INBOX" in svc.msgs["a1"]["labels"]
        assert "UNREAD" in svc.msgs["a2"]["labels"]

    def test_trash_older_than_30_days_cannot_undo(self, tmp_data_dir):
        entry = history.record("trash", ["a1"])
        data = json.loads((tmp_data_dir / "history.json").read_text())
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
        data["entries"][0]["timestamp"] = old.isoformat()
        (tmp_data_dir / "history.json").write_text(json.dumps(data))
        assert history.list_entries()[0]["can_undo"] is False
        assert history.undo(make_inbox(), entry["id"])["success"] is False

    def test_history_is_capped(self):
        for i in range(history.MAX_ENTRIES + 5):
            history.record("archive", [f"id{i}"])
        assert len(history.list_entries()) == history.MAX_ENTRIES

    def test_record_ignores_empty(self):
        assert history.record("trash", []) is None


# ----------------------------------------------------------------- preview


class TestPreview:
    def test_preview_trash(self):
        protected.add("boss@shop.com")
        protected.add("njit.edu")
        svc = make_inbox()
        from app.services.gmail import preview

        with patch.object(preview, "get_gmail_service", return_value=(svc, None)):
            result = preview.preview_action("trash", ["shop.com", "njit.edu"])

        assert result["total"] == 3
        assert result["skipped_senders"] == ["njit.edu"]
        assert len(result["samples"]) == 3
        assert all(s["from"] != "boss@shop.com" for s in result["samples"])
        assert not svc.modify_calls  # dry run

    def test_preview_mark_read_respects_count(self):
        svc = make_inbox()
        from app.services.gmail import preview

        with patch.object(preview, "get_gmail_service", return_value=(svc, None)):
            result = preview.preview_action("mark_read", count=2)
        assert result["total"] == 2


# --------------------------------------------------------- access gate


TEAM = "example.cloudflareaccess.com"
AUD = "aud-tag-123"


@pytest.fixture
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pem_cert = cert.public_bytes(serialization.Encoding.PEM).decode()
    signer = crypt.RSASigner.from_string(pem_key, key_id="kid1")

    def make_token(**overrides):
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        claims = {
            "aud": [AUD],
            "iss": f"https://{TEAM}",
            "email": "me@example.com",
            "iat": now_ts,
            "exp": now_ts + 600,
            **overrides,
        }
        return google_jwt.encode(signer, claims).decode()

    return make_token, {"kid1": pem_cert}


@pytest.fixture
def gated_client(monkeypatch, signing):
    _, certs = signing
    monkeypatch.setattr(settings, "access_team_domain", TEAM)
    monkeypatch.setattr(settings, "access_aud", AUD)
    from app.core import access

    monkeypatch.setattr(access.cert_cache, "get", lambda kid: certs)
    from app.main import app

    return TestClient(app)


class TestAccessGate:
    def test_no_token_blocked(self, gated_client):
        assert gated_client.get("/").status_code == 403
        assert gated_client.get("/api/protected").status_code == 403
        assert gated_client.get("/docs").status_code == 403

    def test_valid_token_allowed(self, gated_client, signing):
        token = signing[0]()
        r = gated_client.get("/api/protected", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "overrides",
        [
            {"aud": ["someone-else"]},
            {"iss": "https://evil.cloudflareaccess.com"},
            {"exp": 1000, "iat": 900},
        ],
    )
    def test_bad_tokens_blocked(self, gated_client, signing, overrides):
        token = signing[0](**overrides)
        r = gated_client.get("/api/protected", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 403

    def test_tampered_token_blocked(self, gated_client, signing):
        token = signing[0]()
        head, body, sig = token.split(".")
        tampered = f"{head}.{body}.{sig[:-4]}AAAA"
        r = gated_client.get("/api/protected", headers={"Cf-Access-Jwt-Assertion": tampered})
        assert r.status_code == 403

    def test_allowed_emails(self, gated_client, signing, monkeypatch):
        monkeypatch.setattr(settings, "access_allowed_emails", "boss@example.com")
        token = signing[0]()
        r = gated_client.get("/api/protected", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 403

    def test_healthz_open(self, gated_client):
        assert gated_client.get("/healthz").json() == {"status": "ok"}


class TestCrossSite:
    def test_cross_site_post_blocked(self):
        from app.main import app

        client = TestClient(app)
        r = client.post(
            "/api/protected",
            json={"sender": "njit.edu"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert r.status_code == 403
        assert protected.load() == []

    def test_same_origin_post_allowed(self):
        from app.main import app

        client = TestClient(app)
        r = client.post(
            "/api/protected",
            json={"sender": "njit.edu"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert r.status_code == 200
        assert r.json() == {"senders": ["njit.edu"]}

    def test_foreign_origin_without_fetch_metadata_blocked(self):
        from app.main import app

        client = TestClient(app)
        r = client.post(
            "/api/protected",
            json={"sender": "njit.edu"},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403


# -------------------------------------------------- pasted OAuth redirect


class TestOAuthPaste:
    @pytest.fixture
    def pending(self):
        from app.services import auth

        event, lock, data = threading.Event(), threading.Lock(), {"code": None, "error": None}
        auth._pending_callback.update(event=event, lock=lock, data=data)
        with state.oauth_state_lock:
            state.oauth_state["state"] = "good-state"
        yield auth, event, data
        auth._pending_callback.update(event=None, lock=None, data=None)
        with state.oauth_state_lock:
            state.oauth_state["state"] = None

    def test_valid_paste_delivers_code(self, pending):
        auth, event, data = pending
        r = auth.submit_pasted_callback(
            "http://localhost:8767/?state=good-state&code=4/abc&scope=x"
        )
        assert r["success"] is True
        assert event.is_set() and data["code"] == "4/abc"

    def test_wrong_state_rejected_and_flow_keeps_waiting(self, pending):
        auth, event, data = pending
        r = auth.submit_pasted_callback("http://localhost:8767/?state=old&code=4/abc")
        assert r["success"] is False
        assert not event.is_set() and data["code"] is None

    def test_no_pending_sign_in(self):
        from app.services import auth

        r = auth.submit_pasted_callback("http://localhost:8767/?state=x&code=y")
        assert r["success"] is False


# ------------------------------------------- sign-in errors reach the page


class TestSignInErrors:
    def test_unreadable_credentials_message(self, tmp_path, monkeypatch):
        from app.services import auth

        # conftest hides files named credentials.json, so use another name
        creds = tmp_path / "client_secret.json"
        creds.write_text('{"installed": {}}')
        monkeypatch.setattr(settings, "credentials_file", str(creds))
        monkeypatch.setattr(settings, "token_file", str(tmp_path / "tok.json"))
        monkeypatch.setattr(auth, "_auth_in_progress", {"active": False})

        real_open = open

        def fake_open(path, *a, **k):
            if str(path) == str(creds):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *a, **k)

        with patch("builtins.open", side_effect=fake_open):
            service, error = auth.get_gmail_service()

        assert service is None
        assert "chown 1000:1000 credentials.json" in error
        assert auth.get_web_auth_status()["auth_error"] == error

    def test_sign_in_api_returns_error(self):
        from app.api import actions
        from app.main import app

        msg = "Can't read credentials.json (Permission denied)."
        with patch.object(actions, "get_gmail_service", return_value=(None, msg)):
            r = TestClient(app).post("/api/sign-in")
        assert r.json() == {"status": "error", "error": msg}

    def test_sign_in_api_started(self):
        from app.api import actions
        from app.main import app

        started = (None, "Sign-in started. Please complete authorization in your browser.")
        with patch.object(actions, "get_gmail_service", return_value=started):
            r = TestClient(app).post("/api/sign-in")
        assert r.json() == {"status": "signing_in"}

    def test_web_auth_status_reports_unwritable_data_dir(self, tmp_path, monkeypatch):
        from app.services import auth

        monkeypatch.setattr(settings, "token_file", str(tmp_path / "tok.json"))
        monkeypatch.setattr(
            auth.os, "access", lambda p, mode: not (mode == auth.os.W_OK and str(p) == str(tmp_path))
        )
        assert auth.get_web_auth_status()["data_writable"] is False
