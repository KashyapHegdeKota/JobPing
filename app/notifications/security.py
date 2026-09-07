"""Credential encryption and Firebase identity verification."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Header, HTTPException


def encrypt(value: str) -> str:
    version = os.environ.get("NOTIFICATION_KEY_VERSION", "v1")
    keys = json.loads(os.environ["NOTIFICATION_ENCRYPTION_KEYS"])
    return version + ":" + Fernet(keys[version].encode()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    version, ciphertext = value.split(":", 1)
    keys = json.loads(os.environ["NOTIFICATION_ENCRYPTION_KEYS"])
    try:
        return Fernet(keys[version].encode()).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Stored credential cannot be decrypted") from None


@dataclass(frozen=True)
class Identity:
    uid: str
    email: str
    verified: bool


def initialize_firebase() -> None:
    from firebase_admin import get_app, initialize_app

    try:
        get_app()
    except ValueError:
        initialize_app(options={"projectId": os.environ["FIREBASE_PROJECT_ID"]})


async def current_recipient(uid: str, email: str) -> bool:
    from firebase_admin import auth

    initialize_firebase()
    try:
        account = await asyncio.to_thread(auth.get_user, uid)
    except auth.UserNotFoundError:
        return False
    return bool(not account.disabled and account.email_verified and account.email == email)


async def identity(authorization: str = Header(default="")) -> Identity:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to manage notifications")
    from firebase_admin import auth

    try:
        initialize_firebase()
    except (KeyError, ValueError):
        raise HTTPException(503, "Email authentication is not configured") from None
    try:
        claims = await asyncio.to_thread(
            auth.verify_id_token, authorization[7:], check_revoked=True
        )
        account = await asyncio.to_thread(auth.get_user, claims["uid"])
    except Exception:
        raise HTTPException(401, "Sign in again to continue") from None
    if account.disabled or not account.email:
        raise HTTPException(403, "An active account with an email is required")
    return Identity(account.uid, account.email, account.email_verified)
