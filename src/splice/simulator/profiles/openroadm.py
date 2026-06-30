# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenROADM simulated device profile.

Provides realistic YANG modules, capabilities, and pre-canned XML
responses for an OpenROADM-compliant ROADM device.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

_NS_DEVICE = "http://org/openroadm/device"
_NS_COMMON = "http://org/openroadm/common-types"
_NS_PM = "http://org/openroadm/pm"
_NS_ALARM = "http://org/openroadm/alarm"
_NS_INTF = "http://org/openroadm/interfaces"
_NS_OTS = "http://org/openroadm/optical-transport-power-specifications"
_NS_LLDP = "http://org/openroadm/lldp"
_NS_NCM = "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring"
_NS_NC = "urn:ietf:params:xml:ns:netconf:base:1.0"

# ---------------------------------------------------------------------------
# YANG modules (what a real OpenROADM device advertises)
# ---------------------------------------------------------------------------

_MODULES: List[Dict[str, object]] = [
    {"name": "org-openroadm-device", "revision": "2023-05-26", "namespace": _NS_DEVICE},
    {"name": "org-openroadm-common-types", "revision": "2023-05-26", "namespace": _NS_COMMON},
    {"name": "org-openroadm-pm", "revision": "2023-05-26", "namespace": _NS_PM},
    {"name": "org-openroadm-alarm", "revision": "2023-05-26", "namespace": _NS_ALARM},
    {"name": "org-openroadm-interfaces", "revision": "2023-05-26", "namespace": _NS_INTF},
    {"name": "org-openroadm-optical-transport-power-specifications", "revision": "2023-05-26", "namespace": _NS_OTS},
    {"name": "org-openroadm-lldp", "revision": "2023-05-26", "namespace": _NS_LLDP},
    {"name": "org-openroadm-common-network", "revision": "2023-05-26", "namespace": "http://org/openroadm/common-network"},
    {"name": "org-openroadm-resource-types", "revision": "2023-05-26", "namespace": "http://org/openroadm/resource-types"},
    {"name": "org-openroadm-wavelength-map", "revision": "2023-05-26", "namespace": "http://org/openroadm/wavelength-map"},
    {"name": "ietf-yang-types", "revision": "2013-07-15", "namespace": "urn:ietf:params:xml:ns:yang:ietf-yang-types"},
    {"name": "ietf-inet-types", "revision": "2013-07-15", "namespace": "urn:ietf:params:xml:ns:yang:ietf-inet-types"},
    {"name": "ietf-netconf-monitoring", "revision": "2010-10-04", "namespace": _NS_NCM},
]

# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

_CAPABILITIES: List[str] = [
    "urn:ietf:params:netconf:base:1.0",
    "urn:ietf:params:netconf:base:1.1",
    "urn:ietf:params:netconf:capability:writable-running:1.0",
    "urn:ietf:params:netconf:capability:candidate:1.0",
    "urn:ietf:params:netconf:capability:confirmed-commit:1.1",
    "urn:ietf:params:netconf:capability:rollback-on-error:1.0",
    "urn:ietf:params:netconf:capability:xpath:1.0",
    "urn:ietf:params:netconf:capability:notification:1.0",
    f"{_NS_DEVICE}?module=org-openroadm-device&revision=2023-05-26",
    f"{_NS_COMMON}?module=org-openroadm-common-types&revision=2023-05-26",
    f"{_NS_PM}?module=org-openroadm-pm&revision=2023-05-26",
    f"{_NS_ALARM}?module=org-openroadm-alarm&revision=2023-05-26",
    f"{_NS_INTF}?module=org-openroadm-interfaces&revision=2023-05-26",
    f"{_NS_OTS}?module=org-openroadm-optical-transport-specifications&revision=2023-05-26",
    f"{_NS_LLDP}?module=org-openroadm-lldp&revision=2023-05-26",
]

# ---------------------------------------------------------------------------
# YANG schema stubs (minimal but parseable by pyang / build_catalog)
# ---------------------------------------------------------------------------

_YANG_SCHEMAS: Dict[str, str] = {
    "org-openroadm-device": """\
module org-openroadm-device {
  namespace "http://org/openroadm/device";
  prefix org-openroadm-device;
  revision 2023-05-26;

  container org-openroadm-device {
    leaf node-id {
      type string;
      config true;
    }
    leaf node-number {
      type uint32;
      config false;
    }
    leaf node-type {
      type string;
      config false;
    }
    leaf vendor {
      type string;
      config false;
    }
    leaf model {
      type string;
      config false;
    }
    leaf software-version {
      type string;
      config false;
    }
    leaf ipAddress {
      type string;
      config true;
    }

    container shelves {
      list shelf {
        key "shelf-name";
        leaf shelf-name {
          type string;
        }
        leaf shelf-type {
          type string;
          config false;
        }
        leaf operational-state {
          type string;
          config false;
        }
      }
    }

    container degree {
      list degree-number {
        key "degree-number";
        leaf degree-number {
          type uint16;
        }
        leaf max-wavelengths {
          type uint16;
          config false;
        }
        container otdr-port {
          leaf port-direction {
            type string;
            config false;
          }
        }
      }
    }

    container shared-risk-group {
      list srg-number {
        key "srg-number";
        leaf srg-number {
          type uint16;
        }
        leaf max-add-drop-ports {
          type uint16;
          config false;
        }
        leaf wavelength-duplication {
          type string;
          config false;
        }
      }
    }

    container interface {
      list interfaces {
        key "name";
        leaf name {
          type string;
        }
        leaf type {
          type string;
          config false;
        }
        leaf administrative-state {
          type string;
          config true;
        }
        leaf operational-state {
          type string;
          config false;
        }
        container optical-power {
          leaf input-power {
            type string;
            config false;
          }
          leaf output-power {
            type string;
            config false;
          }
          leaf target-output-power {
            type string;
            config true;
          }
        }
        container optical-channel {
          leaf frequency {
            type string;
            config false;
          }
          leaf width {
            type string;
            config false;
          }
        }
      }
    }
  }
}
""",
    "org-openroadm-common-types": """\
module org-openroadm-common-types {
  namespace "http://org/openroadm/common-types";
  prefix org-openroadm-common-types;
  revision 2023-05-26;

  typedef node-types {
    type enumeration {
      enum rdm;
      enum xpdr;
      enum ila;
      enum extplug;
    }
  }

  typedef state {
    type enumeration {
      enum inService;
      enum outOfService;
      enum degraded;
    }
  }
}
""",
    "org-openroadm-pm": """\
module org-openroadm-pm {
  namespace "http://org/openroadm/pm";
  prefix org-openroadm-pm;
  revision 2023-05-26;

  container current-pm-list {
    list current-pm-entry {
      key "pm-resource-type pm-resource-name pm-granularity";
      leaf pm-resource-type {
        type string;
      }
      leaf pm-resource-name {
        type string;
      }
      leaf pm-granularity {
        type string;
      }
      leaf pm-value {
        type string;
        config false;
      }
    }
  }
}
""",
    "org-openroadm-alarm": """\
module org-openroadm-alarm {
  namespace "http://org/openroadm/alarm";
  prefix org-openroadm-alarm;
  revision 2023-05-26;

  container active-alarm-list {
    list activeAlarms {
      key "id";
      leaf id {
        type string;
      }
      leaf resource {
        type string;
        config false;
      }
      leaf probable-cause {
        type string;
        config false;
      }
      leaf severity {
        type string;
        config false;
      }
      leaf raise-time {
        type string;
        config false;
      }
    }
  }
}
""",
    "org-openroadm-interfaces": """\
module org-openroadm-interfaces {
  namespace "http://org/openroadm/interfaces";
  prefix org-openroadm-interfaces;
  revision 2023-05-26;

  container interfaces {
    list interface {
      key "name";
      leaf name {
        type string;
      }
      leaf description {
        type string;
        config true;
      }
    }
  }
}
""",
    "org-openroadm-optical-transport-power-specifications": """\
module org-openroadm-optical-transport-power-specifications {
  namespace "http://org/openroadm/optical-transport-power-specifications";
  prefix org-openroadm-ots;
  revision 2023-05-26;

  container optical-transport-specs {
    leaf max-power {
      type string;
      config false;
    }
    leaf min-power {
      type string;
      config false;
    }
  }
}
""",
    "org-openroadm-lldp": """\
module org-openroadm-lldp {
  namespace "http://org/openroadm/lldp";
  prefix org-openroadm-lldp;
  revision 2023-05-26;

  container lldp {
    leaf system-name {
      type string;
      config false;
    }
    leaf system-description {
      type string;
      config false;
    }
  }
}
""",
    "org-openroadm-common-network": """\
module org-openroadm-common-network {
  namespace "http://org/openroadm/common-network";
  prefix org-openroadm-common-network;
  revision 2023-05-26;
}
""",
    "org-openroadm-resource-types": """\
module org-openroadm-resource-types {
  namespace "http://org/openroadm/resource-types";
  prefix org-openroadm-resource-types;
  revision 2023-05-26;
}
""",
    "org-openroadm-wavelength-map": """\
module org-openroadm-wavelength-map {
  namespace "http://org/openroadm/wavelength-map";
  prefix org-openroadm-wavelength-map;
  revision 2023-05-26;

  container wavelength-map {
    list wavelength-entry {
      key "index";
      leaf index {
        type uint32;
      }
      leaf wavelength {
        type string;
        config false;
      }
      leaf frequency {
        type string;
        config false;
      }
    }
  }
}
""",
    "ietf-yang-types": """\
module ietf-yang-types {
  namespace "urn:ietf:params:xml:ns:yang:ietf-yang-types";
  prefix yang;
  revision 2013-07-15;
}
""",
    "ietf-inet-types": """\
module ietf-inet-types {
  namespace "urn:ietf:params:xml:ns:yang:ietf-inet-types";
  prefix inet;
  revision 2013-07-15;
}
""",
    "ietf-netconf-monitoring": """\
module ietf-netconf-monitoring {
  namespace "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring";
  prefix ncm;
  revision 2010-10-04;
}
""",
}

# ---------------------------------------------------------------------------
# Pre-canned XML responses
# ---------------------------------------------------------------------------

_DEVICE_XML = """\
<rpc-reply xmlns="{nc}">
  <data>
    <org-openroadm-device xmlns="{dev}">
      <node-id>ROADM-SIM-01</node-id>
      <node-number>1</node-number>
      <node-type>rdm</node-type>
      <vendor>Simulated</vendor>
      <model>OpenROADM-v13.1</model>
      <software-version>3.2.1</software-version>
      <ipAddress>10.0.0.100</ipAddress>
      <shelves>
        <shelf>
          <shelf-name>shelf-1</shelf-name>
          <shelf-type>SH-20-SLOT</shelf-type>
          <operational-state>inService</operational-state>
        </shelf>
        <shelf>
          <shelf-name>shelf-2</shelf-name>
          <shelf-type>SH-10-SLOT</shelf-type>
          <operational-state>inService</operational-state>
        </shelf>
      </shelves>
      <degree>
        <degree-number>
          <degree-number>1</degree-number>
          <max-wavelengths>96</max-wavelengths>
          <otdr-port>
            <port-direction>bidirectional</port-direction>
          </otdr-port>
        </degree-number>
        <degree-number>
          <degree-number>2</degree-number>
          <max-wavelengths>96</max-wavelengths>
          <otdr-port>
            <port-direction>bidirectional</port-direction>
          </otdr-port>
        </degree-number>
      </degree>
      <shared-risk-group>
        <srg-number>
          <srg-number>1</srg-number>
          <max-add-drop-ports>20</max-add-drop-ports>
          <wavelength-duplication>one-per-srg</wavelength-duplication>
        </srg-number>
      </shared-risk-group>
      <interface>
        <interfaces>
          <name>DEG1-TTP-TXRX</name>
          <type>opticalTransport</type>
          <administrative-state>inService</administrative-state>
          <operational-state>inService</operational-state>
          <optical-power>
            <input-power>-5.2</input-power>
            <output-power>1.3</output-power>
            <target-output-power>1.0</target-output-power>
          </optical-power>
          <optical-channel>
            <frequency>193.1</frequency>
            <width>50.0</width>
          </optical-channel>
        </interfaces>
        <interfaces>
          <name>DEG2-TTP-TXRX</name>
          <type>opticalTransport</type>
          <administrative-state>inService</administrative-state>
          <operational-state>inService</operational-state>
          <optical-power>
            <input-power>-3.8</input-power>
            <output-power>2.1</output-power>
            <target-output-power>2.0</target-output-power>
          </optical-power>
          <optical-channel>
            <frequency>193.2</frequency>
            <width>50.0</width>
          </optical-channel>
        </interfaces>
        <interfaces>
          <name>SRG1-PP1-TXRX</name>
          <type>opticalTransport</type>
          <administrative-state>inService</administrative-state>
          <operational-state>inService</operational-state>
          <optical-power>
            <input-power>-12.4</input-power>
            <output-power>-1.5</output-power>
            <target-output-power>-1.0</target-output-power>
          </optical-power>
          <optical-channel>
            <frequency>193.1</frequency>
            <width>50.0</width>
          </optical-channel>
        </interfaces>
        <interfaces>
          <name>SRG1-PP2-TXRX</name>
          <type>opticalTransport</type>
          <administrative-state>inService</administrative-state>
          <operational-state>degraded</operational-state>
          <optical-power>
            <input-power>-18.7</input-power>
            <output-power>0.4</output-power>
            <target-output-power>0.5</target-output-power>
          </optical-power>
          <optical-channel>
            <frequency>193.3</frequency>
            <width>37.5</width>
          </optical-channel>
        </interfaces>
      </interface>
    </org-openroadm-device>
  </data>
</rpc-reply>""".format(nc=_NS_NC, dev=_NS_DEVICE)


_ALARM_XML = """\
<rpc-reply xmlns="{nc}">
  <data>
    <active-alarm-list xmlns="{alarm}">
      <activeAlarms>
        <id>alarm-001</id>
        <resource>SRG1-PP2-TXRX</resource>
        <probable-cause>opticalPowerDegraded</probable-cause>
        <severity>warning</severity>
        <raise-time>2026-03-12T10:30:00Z</raise-time>
      </activeAlarms>
    </active-alarm-list>
  </data>
</rpc-reply>""".format(nc=_NS_NC, alarm=_NS_ALARM)


_LLDP_XML = """\
<rpc-reply xmlns="{nc}">
  <data>
    <lldp xmlns="{lldp}">
      <system-name>ROADM-SIM-01</system-name>
      <system-description>OpenROADM Simulated Device v13.1</system-description>
    </lldp>
  </data>
</rpc-reply>""".format(nc=_NS_NC, lldp=_NS_LLDP)


_EMPTY_DATA = """\
<rpc-reply xmlns="{nc}">
  <data/>
</rpc-reply>""".format(nc=_NS_NC)

_OK_REPLY = """\
<rpc-reply xmlns="{nc}">
  <ok/>
</rpc-reply>""".format(nc=_NS_NC)


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _filter_mentions(filter_xml: str, *keywords: str) -> bool:
    """Check if the filter XML mentions any of the given keywords."""
    lower = filter_xml.lower()
    return any(kw.lower() in lower for kw in keywords)


# ---------------------------------------------------------------------------
# Profile class
# ---------------------------------------------------------------------------

class OpenROADMProfile:
    """Simulated OpenROADM ROADM device profile."""

    @property
    def name(self) -> str:
        return "openroadm"

    @property
    def capabilities(self) -> List[str]:
        return list(_CAPABILITIES)

    @property
    def modules(self) -> List[Dict[str, object]]:
        return [dict(m) for m in _MODULES]

    def get_response(self, filter_xml: str) -> str:
        if _filter_mentions(filter_xml, "netconf-state", "netconf-monitoring"):
            return self.netconf_monitoring_xml()
        if _filter_mentions(filter_xml, "yang-library", "modules-state"):
            return _EMPTY_DATA
        if _filter_mentions(filter_xml, "active-alarm", "alarm"):
            return _ALARM_XML
        if _filter_mentions(filter_xml, "lldp"):
            return _LLDP_XML
        # Default: return the full device tree
        return _DEVICE_XML

    def get_config_response(self, source: str, filter_xml: str) -> str:
        return _DEVICE_XML

    def get_schema_response(self, identifier: str, version: Optional[str]) -> str:
        yang_text = _YANG_SCHEMAS.get(identifier, "")
        if not yang_text:
            # Return a minimal stub so fetch doesn't fail hard
            ns = ""
            for m in _MODULES:
                if m["name"] == identifier:
                    ns = str(m.get("namespace", ""))
                    break
            yang_text = (
                f"module {identifier} {{\n"
                f'  namespace "{ns}";\n'
                f'  prefix {identifier.split("-")[-1]};\n'
                "}\n"
            )
        escaped = yang_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'<rpc-reply xmlns="{_NS_NC}">'
            f'<data xmlns="{_NS_NCM}">{escaped}</data>'
            f"</rpc-reply>"
        )

    def edit_config_response(self, target: str, config: str) -> str:
        return _OK_REPLY

    def netconf_monitoring_xml(self) -> str:
        schemas_inner = ""
        for m in _MODULES:
            name = m["name"]
            rev = m.get("revision", "")
            ns = m.get("namespace", "")
            schemas_inner += (
                "<schema>"
                f"<identifier>{name}</identifier>"
                f"<version>{rev}</version>"
                "<format>yang</format>"
                f"<namespace>{ns}</namespace>"
                "<location>NETCONF</location>"
                "</schema>"
            )
        return (
            f'<rpc-reply xmlns="{_NS_NC}">'
            "<data>"
            f'<netconf-state xmlns="{_NS_NCM}">'
            f"<schemas>{schemas_inner}</schemas>"
            "</netconf-state>"
            "</data>"
            "</rpc-reply>"
        )
