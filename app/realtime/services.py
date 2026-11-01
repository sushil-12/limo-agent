"""The object graph of the realtime service, built once in the lifespan."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.passport import PassportTokenVerifier
from app.core.rate_limit import InMemoryRateLimiter
from app.realtime.chat import ChatStore
from app.realtime.devices import DeviceRegistry
from app.realtime.events import RideEvents
from app.realtime.fcm import FcmClient
from app.realtime.flags import Flags
from app.realtime.hub import Hub
from app.realtime.laravel import LaravelClient
from app.realtime.location import LocationIngest, TrailSampler
from app.realtime.notify import Notifier
from app.realtime.ocr.documentai import DocumentAiClient
from app.realtime.rides import RideDirectory
from app.realtime.settings import RealtimeSettings
from app.realtime.share import ShareTokens
from app.realtime.status_writer import StatusWriter
from app.realtime.store import RtStore
from app.realtime.tasks import TaskSet


@dataclass
class Realtime:
    settings: RealtimeSettings
    engine: AsyncEngine
    redis: Redis
    http: httpx.AsyncClient
    store: RtStore
    hub: Hub
    verifier: PassportTokenVerifier
    shares: ShareTokens
    rides: RideDirectory
    flags: Flags
    tasks: TaskSet
    devices: DeviceRegistry
    fcm: FcmClient
    laravel: LaravelClient
    notifier: Notifier
    trail: TrailSampler
    location: LocationIngest
    writer: StatusWriter
    chat: ChatStore
    events: RideEvents
    ocr: DocumentAiClient
    ocr_limiter: InMemoryRateLimiter


def build_realtime(
    settings: RealtimeSettings, engine: AsyncEngine, redis: Redis, http: httpx.AsyncClient
) -> Realtime:
    store = RtStore(
        redis,
        stream_max_entries=settings.rt_stream_max_entries,
        stream_ttl_active=settings.rt_stream_ttl_active_seconds,
        stream_ttl_ended=settings.rt_stream_ttl_ended_seconds,
        location_ttl=settings.rt_location_ttl_seconds,
        presence_ttl=settings.rt_presence_ttl_seconds,
    )
    rides = RideDirectory(engine, store, cache_ttl=settings.rt_ride_auth_ttl_seconds)
    flags = Flags(engine, refresh_seconds=settings.rt_flags_refresh_seconds)
    tasks = TaskSet()
    devices = DeviceRegistry(engine)
    fcm = FcmClient(http, project_id=settings.fcm_project_id, credentials=settings.fcm_credentials())
    laravel = LaravelClient(
        http,
        base_url=settings.laravel_api_base_url,
        secret=settings.rt_internal_secret.get_secret_value(),
        ride_completed_path=settings.laravel_ride_completed_path,
        timeout=settings.laravel_timeout_seconds,
    )
    notifier = Notifier(
        engine,
        store,
        devices,
        fcm,
        laravel,
        flags,
        tasks,
        ack_timeout=settings.rt_push_ack_timeout_seconds,
        sms_cap=settings.rt_sms_cap_per_ride,
    )
    trail = TrailSampler(
        store, min_seconds=settings.rt_trail_min_seconds, min_meters=settings.rt_trail_min_meters
    )
    return Realtime(
        settings=settings,
        engine=engine,
        redis=redis,
        http=http,
        store=store,
        hub=Hub(redis),
        verifier=PassportTokenVerifier(
            engine,
            settings.passport_public_key_pem(),
            allowed_roles=settings.rt_auth_allowed_roles,
            leeway_seconds=settings.passport_leeway_seconds,
            cache_ttl_seconds=settings.rt_auth_cache_ttl_seconds,
        ),
        shares=ShareTokens(
            settings.rt_share_token_secret.get_secret_value(), max_hours=settings.rt_share_token_max_hours
        ),
        rides=rides,
        flags=flags,
        tasks=tasks,
        devices=devices,
        fcm=fcm,
        laravel=laravel,
        notifier=notifier,
        trail=trail,
        location=LocationIngest(store, rides, trail),
        writer=StatusWriter(engine),
        chat=ChatStore(engine),
        events=RideEvents(store, rides, notifier, laravel, trail, tasks),
        ocr=DocumentAiClient(
            http,
            project_id=settings.documentai_project_id,
            location=settings.documentai_location,
            processor_id=settings.documentai_processor_id,
            credentials=settings.documentai_credentials(),
            timeout_seconds=settings.documentai_timeout_ms / 1000,
        ),
        ocr_limiter=InMemoryRateLimiter(settings.rt_ocr_rate_per_minute),
    )
