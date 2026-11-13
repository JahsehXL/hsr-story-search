"""Validate Cloudflare Access identities at the origin, independently of proxy headers."""
from __future__ import annotations

import re
import threading
from http.cookies import CookieError, SimpleCookie

import jwt


class AccessDenied(Exception):
    pass


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str, allowed_emails: list[str]):
        self.issuer = team_domain.strip().rstrip("/")
        if not re.fullmatch(r"https://[a-z0-9-]+\.cloudflareaccess\.com", self.issuer):
            raise ValueError("Cloudflare Access requires an HTTPS team domain")
        if not audience.strip() or not allowed_emails:
            raise ValueError("Cloudflare Access requires an audience and allowed email addresses")
        self.audience = audience.strip()
        self.emails = {email.strip().lower() for email in allowed_emails if email.strip()}
        if not self.emails:
            raise ValueError("Cloudflare Access email allowlist is empty")
        self.keys = jwt.PyJWKClient(self.issuer + "/cdn-cgi/access/certs", lifespan=300, timeout=5)
        self.lock = threading.Lock()

    def verify(self, headers) -> dict:
        token = headers.get("Cf-Access-Jwt-Assertion", "").strip()
        if not token:
            cookie = SimpleCookie()
            try:
                cookie.load(headers.get("Cookie", ""))
                token = cookie["CF_Authorization"].value if "CF_Authorization" in cookie else ""
            except CookieError:
                raise AccessDenied("Cloudflare Access authentication required") from None
        if not token or len(token) > 16384:
            raise AccessDenied("Cloudflare Access authentication required")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or len(header["kid"]) > 256:
                raise AccessDenied("Invalid Cloudflare Access token")
            # Serialize key-cache refreshes across concurrent requests.
            with self.lock:
                key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]},
            )
        except (jwt.PyJWTError, OSError, ValueError, TypeError):
            raise AccessDenied("Invalid or expired Cloudflare Access token") from None
        email = str(claims.get("email", "")).strip().lower()
        if email not in self.emails:
            raise AccessDenied("This account is not allowed to administer this site")
        return {"email": email, "subject": claims["sub"], "expires": claims["exp"]}
