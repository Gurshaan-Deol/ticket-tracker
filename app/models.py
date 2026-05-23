from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    venue: Mapped[str | None] = mapped_column(String, nullable=True)
    event_date: Mapped[str | None] = mapped_column(String, nullable=True)
    ticketmaster_url: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    available_quantities: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    listings: Mapped[list["Listing"]] = relationship("Listing", back_populates="event")


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    event: Mapped["Event"] = relationship("Event", back_populates="listings")
    watches: Mapped[list["UserWatch"]] = relationship("UserWatch", back_populates="listing")
    snapshots: Mapped[list["PriceSnapshot"]] = relationship("PriceSnapshot", back_populates="listing")
    alert_logs: Mapped[list["AlertLog"]] = relationship("AlertLog", back_populates="listing")


class UserWatch(Base):
    __tablename__ = "user_watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, ForeignKey("listings.id"), nullable=False)
    target_price: Mapped[float] = mapped_column(Float, nullable=False)
    refresh_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    alert_cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    listing: Mapped["Listing"] = relationship("Listing", back_populates="watches")


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, ForeignKey("listings.id"), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    listing: Mapped["Listing"] = relationship("Listing", back_populates="snapshots")


class AlertLog(Base):
    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, ForeignKey("listings.id"), nullable=False)
    price_at_alert: Mapped[float] = mapped_column(Float, nullable=False)
    alerted_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    channels_used: Mapped[str] = mapped_column(String, nullable=False)

    listing: Mapped["Listing"] = relationship("Listing", back_populates="alert_logs")


Index("ix_listings_event_id", Listing.event_id)
Index("ix_user_watches_listing_id", UserWatch.listing_id)
Index("ix_price_snapshots_listing_id", PriceSnapshot.listing_id)
Index("ix_alert_log_listing_alerted", AlertLog.listing_id, AlertLog.alerted_at)
