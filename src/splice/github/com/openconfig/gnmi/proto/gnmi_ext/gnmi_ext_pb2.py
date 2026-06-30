"""Minimal gnmi_ext protobuf definitions used by splice.

This file intentionally defines only what the bridge needs:
  - Extension.registered_ext
  - RegisteredExtension {id, msg}

It is sufficient for custom gNMI extension handling without requiring
full upstream gnmi_ext proto generation.
"""
from __future__ import annotations

from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message_factory as _message_factory
from google.protobuf import symbol_database as _symbol_database

_sym_db = _symbol_database.Default()


_fdp = _descriptor_pb2.FileDescriptorProto()
_fdp.name = "github.com/openconfig/gnmi/proto/gnmi_ext/gnmi_ext.proto"
_fdp.package = "gnmi_ext"
_fdp.syntax = "proto3"

_registered_ext = _fdp.message_type.add()
_registered_ext.name = "RegisteredExtension"

_id_field = _registered_ext.field.add()
_id_field.name = "id"
_id_field.number = 1
_id_field.label = _descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
_id_field.type = _descriptor_pb2.FieldDescriptorProto.TYPE_UINT32

_msg_field = _registered_ext.field.add()
_msg_field.name = "msg"
_msg_field.number = 2
_msg_field.label = _descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
_msg_field.type = _descriptor_pb2.FieldDescriptorProto.TYPE_BYTES

_extension = _fdp.message_type.add()
_extension.name = "Extension"
_ext_oneof = _extension.oneof_decl.add()
_ext_oneof.name = "ext"

_reg_field = _extension.field.add()
_reg_field.name = "registered_ext"
_reg_field.number = 2
_reg_field.label = _descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
_reg_field.type = _descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
_reg_field.type_name = ".gnmi_ext.RegisteredExtension"
_reg_field.oneof_index = 0


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(_fdp.SerializeToString())

Extension = _message_factory.GetMessageClass(DESCRIPTOR.message_types_by_name["Extension"])
RegisteredExtension = _message_factory.GetMessageClass(
    DESCRIPTOR.message_types_by_name["RegisteredExtension"]
)

# Backward-compatible alias for previous stub name.
RegisteredExt = RegisteredExtension

_sym_db.RegisterMessage(Extension)
_sym_db.RegisterMessage(RegisteredExtension)
