# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Device(Base):
    __tablename__ = "devices"

    id = Column(BigInteger, primary_key=True)
    name = Column(String(255), nullable=True)
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False, default=830)
    username = Column(String(255), nullable=False)
    password_ref = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (Index("ix_devices_host_port", "host", "port", unique=True),)


class Module(Base):
    __tablename__ = "modules"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    revision = Column(String(64), nullable=True)
    namespace = Column(Text, nullable=True)
    format = Column(String(32), nullable=True)
    source = Column(String(64), nullable=True)
    conformance = Column(String(64), nullable=True)
    features = Column(JSONB, nullable=True)
    deviations = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_modules_device_name_rev", "device_id", "name", "revision", unique=True),
    )


class YangFile(Base):
    __tablename__ = "yang_files"

    id = Column(BigInteger, primary_key=True)
    module_id = Column(BigInteger, ForeignKey("modules.id", ondelete="CASCADE"), nullable=False)
    path = Column(Text, nullable=False)
    checksum = Column(String(128), nullable=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_yang_files_module", "module_id"),)


class CatalogPath(Base):
    __tablename__ = "catalog_paths"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    module_id = Column(BigInteger, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    canonical_path = Column(Text, nullable=False)
    filter_xml = Column(Text, nullable=False)
    namespace_map = Column(JSONB, nullable=True)
    list_keys = Column(JSONB, nullable=True)
    keyword = Column(String(32), nullable=False)  # container/list/leaf/leaf-list
    config = Column(Boolean, nullable=False, default=False)
    data_type = Column(String(128), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_catalog_paths_device_path", "device_id", "canonical_path", unique=True),
    )


class PathAlias(Base):
    __tablename__ = "path_aliases"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    canonical_path = Column(Text, nullable=False)
    alias_path = Column(Text, nullable=False)
    source = Column(String(64), nullable=False, default="generated")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_path_aliases_device_alias", "device_id", "alias_path", unique=True),
        Index("ix_path_aliases_device_canon", "device_id", "canonical_path"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    canonical_path = Column(Text, nullable=False)
    interval_sec = Column(Integer, nullable=False, default=30)
    enabled = Column(Boolean, nullable=False, default=True)
    mode = Column(String(32), nullable=False, default="sample")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_jobs_device_path", "device_id", "canonical_path", unique=True),
    )


class ListInstance(Base):
    __tablename__ = "list_instances"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    list_path = Column(Text, nullable=False)
    key_values = Column(JSONB, nullable=False)
    discovered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_list_instances_device_list", "device_id", "list_path"),
        Index("ix_list_instances_device_list_keys", "device_id", "list_path", "key_values", unique=True),
    )


class PMFilter(Base):
    __tablename__ = "pm_filters"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    root_path = Column(Text, nullable=False)
    filter_xml = Column(Text, nullable=False)
    list_path = Column(Text, nullable=True)
    key_values = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_pm_filters_device_root", "device_id", "root_path"),
        Index(
            "ix_pm_filters_device_root_keys",
            "device_id",
            "root_path",
            "list_path",
            "key_values",
            unique=True,
        ),
    )


class LastValue(Base):
    __tablename__ = "last_value"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    canonical_path = Column(Text, nullable=False)
    value_json = Column(JSONB, nullable=True)
    timestamp_ns = Column(BigInteger, nullable=False, default=0)
    meta = Column(JSONB, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_last_value_device_path", "device_id", "canonical_path", unique=True),
    )


class TelemetryHistory(Base):
    __tablename__ = "telemetry_history"

    id = Column(BigInteger, primary_key=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    canonical_path = Column(Text, nullable=False)
    value_json = Column(JSONB, nullable=True)
    timestamp_ns = Column(BigInteger, nullable=False, default=0)
    meta = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_hist_device_path_ts", "device_id", "canonical_path", "timestamp_ns"),
    )
