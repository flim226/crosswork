#!/usr/bin/env python3
"""
Mock CNC (Crosswork Network Controller) server for testing get_plan.py.

Usage:
    python fake_cnc_server.py [--port PORT] [--plan-file PLAN_FILE]

Then run get_plan.py against it (-k because the certificate is self-signed):
    python get_plan.py --ip 127.0.0.1 -u admin -p admin -k -f plan.txt
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SSO_TICKETS_PATH = "/crosswork/sso/v1/tickets"
SSO_JWT_PATH = "/crosswork/sso/v2/tickets/jwt"
GET_PLAN_PATH = (
    "/crosswork/nbi/optima/v2/restconf/operations/"
    "cisco-crosswork-optimization-engine-operations:get-plan"
)
INVENTORY_QUERY_PATH = "/crosswork/inventory/v1/nodes/query"

# Fixed secret so JWTs cached in ~/.crosswork/<ip>.jwt stay valid across restarts.
JWT_SECRET = b"fake-cnc-server-secret"
JWT_LIFETIME_SECONDS = 8 * 3600

DEFAULT_PLAN = """<Network>
Property\tValue
Title\t
Version\t6.4

<Sites>
Name\tLocationCode\tLongitude\tLatitude
atl\tATL\t-84.43\t33.65
bos\tBOS\t-71.0\t42.37
chi\tCHI\t-87.77\t41.88
hst\tHOU\t-95.3\t29.65
kcy\tMCI\t-94.73\t39.29
lax\tLAX\t-118.4\t33.93
mia\tMIA\t-80.28\t25.8
nyc\tJFK\t-73.78\t40.63
sea\tSEA\t-122.3\t47.45
sjc\tSJC\t-121.92\t37.35
wdc\tDCA\t-77.03\t38.85

<Nodes>
Name\tSite\tFunction\tProtected
cr1.atl\tatl\tcore\tF
cr1.bos\tbos\tcore\tF
cr1.chi\tchi\tcore\tF
cr1.hst\thst\tcore\tF
cr1.kcy\tkcy\tcore\tF
cr1.lax\tlax\tcore\tF
cr1.mia\tmia\tcore\tF
cr1.nyc\tnyc\tcore\tF
cr1.sea\tsea\tcore\tF
cr1.sjc\tsjc\tcore\tF
cr1.wdc\twdc\tcore\tF
cr2.atl\tatl\tcore\tF
cr2.bos\tbos\tcore\tF
cr2.chi\tchi\tcore\tF
cr2.hst\thst\tcore\tF
cr2.kcy\tkcy\tcore\tF
cr2.lax\tlax\tcore\tF
cr2.mia\tmia\tcore\tF
cr2.nyc\tnyc\tcore\tF
cr2.sea\tsea\tcore\tF
cr2.sjc\tsjc\tcore\tF
cr2.wdc\twdc\tcore\tF
er1.atl\tatl\tedge\tT
er1.bos\tbos\tedge\tT
er1.chi\tchi\tedge\tT
er1.hst\thst\tedge\tT
er1.kcy\tkcy\tedge\tT
er1.lax\tlax\tedge\tT
er1.mia\tmia\tedge\tT
er1.nyc\tnyc\tedge\tT
er1.sea\tsea\tedge\tT
er1.sjc\tsjc\tedge\tT
er1.wdc\twdc\tedge\tT

<Circuits>
Name\tNodeA\tInterfaceA\tNodeB\tInterfaceB\tCapacity\tDelay\tProtected
cr1.atl-cr1.hst\tcr1.atl\tto_cr1.hst\tcr1.hst\tto_cr1.atl\t2488.0\t7.3\tF
cr1.sjc-cr1.kcy\tcr1.kcy\tto_cr1.sjc\tcr1.sjc\tto_cr1.kcy\t2488.0\t14.7\tF
cr1.kcy-cr1.wdc\tcr1.kcy\tto_cr1.wdc\tcr1.wdc\tto_cr1.kcy\t2488.0\t9.5\tF
cr1.atl-cr2.atl\tcr1.atl\tto_cr2.atl\tcr2.atl\tto_cr1.atl\t10000.0\t0.0\tF
cr2.atl-cr1.wdc\tcr1.wdc\tto_cr2.atl\tcr2.atl\tto_cr1.wdc\t2488.0\t6.6\tF
cr1.bos-cr2.bos\tcr1.bos\tto_cr2.bos\tcr2.bos\tto_cr1.bos\t10000.0\t0.0\tF
cr2.bos-cr1.nyc\tcr1.nyc\tto_cr2.bos\tcr2.bos\tto_cr1.nyc\t2488.0\t2.2\tF
cr1.bos-cr2.chi\tcr1.bos\tto_cr2.chi\tcr2.chi\tto_cr1.bos\t2488.0\t8.6\tF
cr1.chi-cr2.chi\tcr1.chi\tto_cr2.chi\tcr2.chi\tto_cr1.chi\t10000.0\t0.0\tF
cr2.chi-cr1.sjc\tcr1.sjc\tto_cr2.chi\tcr2.chi\tto_cr1.sjc\t2488.0\t18.3\tF
cr1.hst-cr2.hst\tcr1.hst\tto_cr2.hst\tcr2.hst\tto_cr1.hst\t10000.0\t0.0\tF
cr2.hst-cr1.lax\tcr1.lax\tto_cr2.hst\tcr2.hst\tto_cr1.lax\t2488.0\t23.4\tF
cr2.kcy-cr1.hst\tcr1.hst\tto_cr2.kcy\tcr2.kcy\tto_cr1.hst\t2488.0\t12.6\tF
cr1.kcy-cr2.kcy\tcr1.kcy\tto_cr2.kcy\tcr2.kcy\tto_cr1.kcy\t10000.0\t0.0\tF
cr2.chi-cr2.kcy\tcr2.chi\tto_cr2.kcy\tcr2.kcy\tto_cr2.chi\t2488.0\t4.9\tF
cr1.lax-cr2.lax\tcr1.lax\tto_cr2.lax\tcr2.lax\tto_cr1.lax\t10000.0\t0.0\tF
cr1.atl-cr2.mia\tcr1.atl\tto_cr2.mia\tcr2.mia\tto_cr1.atl\t2488.0\t7.2\tF
cr1.mia-cr2.mia\tcr1.mia\tto_cr2.mia\tcr2.mia\tto_cr1.mia\t10000.0\t0.0\tF
cr1.chi-cr2.nyc\tcr1.chi\tto_cr2.nyc\tcr2.nyc\tto_cr1.chi\t2488.0\t7.5\tF
cr1.nyc-cr2.nyc\tcr1.nyc\tto_cr2.nyc\tcr2.nyc\tto_cr1.nyc\t10000.0\t0.0\tF
cr1.chi-cr2.sea\tcr1.chi\tto_cr2.sea\tcr2.sea\tto_cr1.chi\t2488.0\t17.3\tF
cr1.sea-cr2.sea\tcr1.sea\tto_cr2.sea\tcr2.sea\tto_cr1.sea\t10000.0\t0.0\tF
cr2.sjc-cr1.sea\tcr1.sea\tto_cr2.sjc\tcr2.sjc\tto_cr1.sea\t2488.0\t7.5\tF
cr1.sjc-cr2.sjc\tcr1.sjc\tto_cr2.sjc\tcr2.sjc\tto_cr1.sjc\t10000.0\t0.0\tF
cr2.lax-cr2.sjc\tcr2.lax\tto_cr2.sjc\tcr2.sjc\tto_cr2.lax\t2488.0\t3.7\tF
cr1.mia-cr2.wdc\tcr1.mia\tto_cr2.wdc\tcr2.wdc\tto_cr1.mia\t2488.0\t9.19\tF
cr1.nyc-cr2.wdc\tcr1.nyc\tto_cr2.wdc\tcr2.wdc\tto_cr1.nyc\t2488.0\t2.5\tF
cr1.wdc-cr2.wdc\tcr1.wdc\tto_cr2.wdc\tcr2.wdc\tto_cr1.wdc\t10000.0\t0.0\tF
er1.atl-cr1.atl\tcr1.atl\tto_er1.atl\ter1.atl\tto_cr1.atl\t\t0.0\tT
er1.atl-cr2.atl\tcr2.atl\tto_er1.atl\ter1.atl\tto_cr2.atl\t\t0.0\tT
er1.bos-cr1.bos\tcr1.bos\tto_er1.bos\ter1.bos\tto_cr1.bos\t\t0.0\tT
er1.bos-cr2.bos\tcr2.bos\tto_er1.bos\ter1.bos\tto_cr2.bos\t\t0.0\tT
er1.chi-cr1.chi\tcr1.chi\tto_er1.chi\ter1.chi\tto_cr1.chi\t\t0.0\tT
er1.chi-cr2.chi\tcr2.chi\tto_er1.chi\ter1.chi\tto_cr2.chi\t\t0.0\tT
er1.hst-cr1.hst\tcr1.hst\tto_er1.hst\ter1.hst\tto_cr1.hst\t\t0.0\tT
er1.hst-cr2.hst\tcr2.hst\tto_er1.hst\ter1.hst\tto_cr2.hst\t\t0.0\tT
er1.kcy-cr1.kcy\tcr1.kcy\tto_er1.kcy\ter1.kcy\tto_cr1.kcy\t\t0.0\tT
er1.kcy-cr2.kcy\tcr2.kcy\tto_er1.kcy\ter1.kcy\tto_cr2.kcy\t\t0.0\tT
er1.lax-cr1.lax\tcr1.lax\tto_er1.lax\ter1.lax\tto_cr1.lax\t\t0.0\tT
er1.lax-cr2.lax\tcr2.lax\tto_er1.lax\ter1.lax\tto_cr2.lax\t\t0.0\tT
er1.mia-cr1.mia\tcr1.mia\tto_er1.mia\ter1.mia\tto_cr1.mia\t\t0.0\tT
er1.mia-cr2.mia\tcr2.mia\tto_er1.mia\ter1.mia\tto_cr2.mia\t\t0.0\tT
er1.nyc-cr1.nyc\tcr1.nyc\tto_er1.nyc\ter1.nyc\tto_cr1.nyc\t\t0.0\tT
er1.nyc-cr2.nyc\tcr2.nyc\tto_er1.nyc\ter1.nyc\tto_cr2.nyc\t\t0.0\tT
er1.sea-cr1.sea\tcr1.sea\tto_er1.sea\ter1.sea\tto_cr1.sea\t\t0.0\tT
er1.sea-cr2.sea\tcr2.sea\tto_er1.sea\ter1.sea\tto_cr2.sea\t\t0.0\tT
er1.sjc-cr1.sjc\tcr1.sjc\tto_er1.sjc\ter1.sjc\tto_cr1.sjc\t\t0.0\tT
er1.sjc-cr2.sjc\tcr2.sjc\tto_er1.sjc\ter1.sjc\tto_cr2.sjc\t\t0.0\tT
er1.wdc-cr1.wdc\tcr1.wdc\tto_er1.wdc\ter1.wdc\tto_cr1.wdc\t\t0.0\tT
er1.wdc-cr2.wdc\tcr2.wdc\tto_er1.wdc\ter1.wdc\tto_cr2.wdc\t\t0.0\tT

<Interfaces>
Node\tInterface\tIGPMetric
cr1.atl\tto_cr1.hst\t37
cr1.atl\tto_cr2.atl\t1
cr1.atl\tto_cr2.mia\t36
cr1.atl\tto_er1.atl\t1000
cr1.bos\tto_cr2.bos\t1
cr1.bos\tto_cr2.chi\t43
cr1.bos\tto_er1.bos\t1000
cr1.chi\tto_cr2.chi\t1
cr1.chi\tto_cr2.nyc\t38
cr1.chi\tto_cr2.sea\t87
cr1.chi\tto_er1.chi\t1000
cr1.hst\tto_cr1.atl\t37
cr1.hst\tto_cr2.hst\t1
cr1.hst\tto_cr2.kcy\t63
cr1.hst\tto_er1.hst\t1000
cr1.kcy\tto_cr1.sjc\t74
cr1.kcy\tto_cr1.wdc\t48
cr1.kcy\tto_cr2.kcy\t1
cr1.kcy\tto_er1.kcy\t1000
cr1.lax\tto_cr2.hst\t117
cr1.lax\tto_cr2.lax\t1
cr1.lax\tto_er1.lax\t1000
cr1.mia\tto_cr2.mia\t1
cr1.mia\tto_cr2.wdc\t46
cr1.mia\tto_er1.mia\t1000
cr1.nyc\tto_cr2.bos\t11
cr1.nyc\tto_cr2.nyc\t1
cr1.nyc\tto_cr2.wdc\t13
cr1.nyc\tto_er1.nyc\t1000
cr1.sea\tto_cr2.sea\t1
cr1.sea\tto_cr2.sjc\t38
cr1.sea\tto_er1.sea\t1000
cr1.sjc\tto_cr1.kcy\t74
cr1.sjc\tto_cr2.chi\t92
cr1.sjc\tto_cr2.sjc\t1
cr1.sjc\tto_er1.sjc\t1000
cr1.wdc\tto_cr1.kcy\t48
cr1.wdc\tto_cr2.atl\t33
cr1.wdc\tto_cr2.wdc\t1
cr1.wdc\tto_er1.wdc\t1000
cr2.atl\tto_cr1.atl\t1
cr2.atl\tto_cr1.wdc\t33
cr2.atl\tto_er1.atl\t1000
cr2.bos\tto_cr1.bos\t1
cr2.bos\tto_cr1.nyc\t11
cr2.bos\tto_er1.bos\t1000
cr2.chi\tto_cr1.bos\t43
cr2.chi\tto_cr1.chi\t1
cr2.chi\tto_cr1.sjc\t92
cr2.chi\tto_cr2.kcy\t25
cr2.chi\tto_er1.chi\t1000
cr2.hst\tto_cr1.hst\t1
cr2.hst\tto_cr1.lax\t117
cr2.hst\tto_er1.hst\t1000
cr2.kcy\tto_cr1.hst\t63
cr2.kcy\tto_cr1.kcy\t1
cr2.kcy\tto_cr2.chi\t25
cr2.kcy\tto_er1.kcy\t1000
cr2.lax\tto_cr1.lax\t1
cr2.lax\tto_cr2.sjc\t19
cr2.lax\tto_er1.lax\t1000
cr2.mia\tto_cr1.atl\t36
cr2.mia\tto_cr1.mia\t1
cr2.mia\tto_er1.mia\t1000
cr2.nyc\tto_cr1.chi\t38
cr2.nyc\tto_cr1.nyc\t1
cr2.nyc\tto_er1.nyc\t1000
cr2.sea\tto_cr1.chi\t87
cr2.sea\tto_cr1.sea\t1
cr2.sea\tto_er1.sea\t1000
cr2.sjc\tto_cr1.sea\t38
cr2.sjc\tto_cr1.sjc\t1
cr2.sjc\tto_cr2.lax\t19
cr2.sjc\tto_er1.sjc\t1000
cr2.wdc\tto_cr1.mia\t46
cr2.wdc\tto_cr1.nyc\t13
cr2.wdc\tto_cr1.wdc\t1
cr2.wdc\tto_er1.wdc\t1000
er1.atl\tto_cr1.atl\t1000
er1.atl\tto_cr2.atl\t1000
er1.bos\tto_cr1.bos\t1000
er1.bos\tto_cr2.bos\t1000
er1.chi\tto_cr1.chi\t1000
er1.chi\tto_cr2.chi\t1000
er1.hst\tto_cr1.hst\t1000
er1.hst\tto_cr2.hst\t1000
er1.kcy\tto_cr1.kcy\t1000
er1.kcy\tto_cr2.kcy\t1000
er1.lax\tto_cr1.lax\t1000
er1.lax\tto_cr2.lax\t1000
er1.mia\tto_cr1.mia\t1000
er1.mia\tto_cr2.mia\t1000
er1.nyc\tto_cr1.nyc\t1000
er1.nyc\tto_cr2.nyc\t1000
er1.sea\tto_cr1.sea\t1000
er1.sea\tto_cr2.sea\t1000
er1.sjc\tto_cr1.sjc\t1000
er1.sjc\tto_cr2.sjc\t1000
er1.wdc\tto_cr1.wdc\t1000
er1.wdc\tto_cr2.wdc\t1000

<ServiceClasses>
Name
Default

<Demands>
Name\tSource\tDestination\tServiceClass
er1.atl_er1.bos\ter1.atl\ter1.bos\tDefault
er1.atl_er1.chi\ter1.atl\ter1.chi\tDefault
er1.atl_er1.hst\ter1.atl\ter1.hst\tDefault
er1.atl_er1.lax\ter1.atl\ter1.lax\tDefault
er1.atl_er1.mia\ter1.atl\ter1.mia\tDefault
er1.atl_er1.nyc\ter1.atl\ter1.nyc\tDefault
er1.atl_er1.sea\ter1.atl\ter1.sea\tDefault
er1.atl_er1.sjc\ter1.atl\ter1.sjc\tDefault
er1.atl_er1.wdc\ter1.atl\ter1.wdc\tDefault
er1.bos_er1.atl\ter1.bos\ter1.atl\tDefault
er1.bos_er1.chi\ter1.bos\ter1.chi\tDefault
er1.bos_er1.hst\ter1.bos\ter1.hst\tDefault
er1.bos_er1.lax\ter1.bos\ter1.lax\tDefault
er1.bos_er1.mia\ter1.bos\ter1.mia\tDefault
er1.bos_er1.nyc\ter1.bos\ter1.nyc\tDefault
er1.bos_er1.sea\ter1.bos\ter1.sea\tDefault
er1.bos_er1.sjc\ter1.bos\ter1.sjc\tDefault
er1.bos_er1.wdc\ter1.bos\ter1.wdc\tDefault
er1.chi_er1.atl\ter1.chi\ter1.atl\tDefault
er1.chi_er1.bos\ter1.chi\ter1.bos\tDefault
er1.chi_er1.hst\ter1.chi\ter1.hst\tDefault
er1.chi_er1.lax\ter1.chi\ter1.lax\tDefault
er1.chi_er1.mia\ter1.chi\ter1.mia\tDefault
er1.chi_er1.nyc\ter1.chi\ter1.nyc\tDefault
er1.chi_er1.sea\ter1.chi\ter1.sea\tDefault
er1.chi_er1.sjc\ter1.chi\ter1.sjc\tDefault
er1.chi_er1.wdc\ter1.chi\ter1.wdc\tDefault
er1.hst_er1.atl\ter1.hst\ter1.atl\tDefault
er1.hst_er1.bos\ter1.hst\ter1.bos\tDefault
er1.hst_er1.chi\ter1.hst\ter1.chi\tDefault
er1.hst_er1.lax\ter1.hst\ter1.lax\tDefault
er1.hst_er1.mia\ter1.hst\ter1.mia\tDefault
er1.hst_er1.nyc\ter1.hst\ter1.nyc\tDefault
er1.hst_er1.sea\ter1.hst\ter1.sea\tDefault
er1.hst_er1.sjc\ter1.hst\ter1.sjc\tDefault
er1.hst_er1.wdc\ter1.hst\ter1.wdc\tDefault
er1.lax_er1.atl\ter1.lax\ter1.atl\tDefault
er1.lax_er1.bos\ter1.lax\ter1.bos\tDefault
er1.lax_er1.chi\ter1.lax\ter1.chi\tDefault
er1.lax_er1.hst\ter1.lax\ter1.hst\tDefault
er1.lax_er1.lax\ter1.lax\ter1.lax\tDefault
er1.lax_er1.mia\ter1.lax\ter1.mia\tDefault
er1.lax_er1.nyc\ter1.lax\ter1.nyc\tDefault
er1.lax_er1.sea\ter1.lax\ter1.sea\tDefault
er1.lax_er1.sjc\ter1.lax\ter1.sjc\tDefault
er1.lax_er1.wdc\ter1.lax\ter1.wdc\tDefault
er1.mia_er1.atl\ter1.mia\ter1.atl\tDefault
er1.mia_er1.bos\ter1.mia\ter1.bos\tDefault
er1.mia_er1.chi\ter1.mia\ter1.chi\tDefault
er1.mia_er1.hst\ter1.mia\ter1.hst\tDefault
er1.mia_er1.lax\ter1.mia\ter1.lax\tDefault
er1.mia_er1.mia\ter1.mia\ter1.mia\tDefault
er1.mia_er1.nyc\ter1.mia\ter1.nyc\tDefault
er1.mia_er1.sea\ter1.mia\ter1.sea\tDefault
er1.mia_er1.sjc\ter1.mia\ter1.sjc\tDefault
er1.mia_er1.wdc\ter1.mia\ter1.wdc\tDefault
er1.nyc_er1.atl\ter1.nyc\ter1.atl\tDefault
er1.nyc_er1.bos\ter1.nyc\ter1.bos\tDefault
er1.nyc_er1.chi\ter1.nyc\ter1.chi\tDefault
er1.nyc_er1.hst\ter1.nyc\ter1.hst\tDefault
er1.nyc_er1.lax\ter1.nyc\ter1.lax\tDefault
er1.nyc_er1.mia\ter1.nyc\ter1.mia\tDefault
er1.nyc_er1.sea\ter1.nyc\ter1.sea\tDefault
er1.nyc_er1.sjc\ter1.nyc\ter1.sjc\tDefault
er1.nyc_er1.wdc\ter1.nyc\ter1.wdc\tDefault
er1.sea_er1.atl\ter1.sea\ter1.atl\tDefault
er1.sea_er1.bos\ter1.sea\ter1.bos\tDefault
er1.sea_er1.chi\ter1.sea\ter1.chi\tDefault
er1.sea_er1.hst\ter1.sea\ter1.hst\tDefault
er1.sea_er1.lax\ter1.sea\ter1.lax\tDefault
er1.sea_er1.mia\ter1.sea\ter1.mia\tDefault
er1.sea_er1.nyc\ter1.sea\ter1.nyc\tDefault
er1.sea_er1.sea\ter1.sea\ter1.sea\tDefault
er1.sea_er1.sjc\ter1.sea\ter1.sjc\tDefault
er1.sea_er1.wdc\ter1.sea\ter1.wdc\tDefault
er1.sjc_er1.atl\ter1.sjc\ter1.atl\tDefault
er1.sjc_er1.bos\ter1.sjc\ter1.bos\tDefault
er1.sjc_er1.chi\ter1.sjc\ter1.chi\tDefault
er1.sjc_er1.hst\ter1.sjc\ter1.hst\tDefault
er1.sjc_er1.lax\ter1.sjc\ter1.lax\tDefault
er1.sjc_er1.mia\ter1.sjc\ter1.mia\tDefault
er1.sjc_er1.nyc\ter1.sjc\ter1.nyc\tDefault
er1.sjc_er1.sea\ter1.sjc\ter1.sea\tDefault
er1.sjc_er1.sjc\ter1.sjc\ter1.sjc\tDefault
er1.sjc_er1.wdc\ter1.sjc\ter1.wdc\tDefault
er1.wdc_er1.atl\ter1.wdc\ter1.atl\tDefault
er1.wdc_er1.bos\ter1.wdc\ter1.bos\tDefault
er1.wdc_er1.chi\ter1.wdc\ter1.chi\tDefault
er1.wdc_er1.hst\ter1.wdc\ter1.hst\tDefault
er1.wdc_er1.lax\ter1.wdc\ter1.lax\tDefault
er1.wdc_er1.mia\ter1.wdc\ter1.mia\tDefault
er1.wdc_er1.nyc\ter1.wdc\ter1.nyc\tDefault
er1.wdc_er1.sea\ter1.wdc\ter1.sea\tDefault
er1.wdc_er1.sjc\ter1.wdc\ter1.sjc\tDefault
er1.wdc_er1.wdc\ter1.wdc\ter1.wdc\tDefault

<TrafficLevels>
Name
Default

<DemandTraffic>
Name\tSource\tDestination\tServiceClass\tTrafficLevel\tTraffic
er1.atl_er1.bos\ter1.atl\ter1.bos\tDefault\tDefault\t73.33
er1.atl_er1.chi\ter1.atl\ter1.chi\tDefault\tDefault\t105.71
er1.atl_er1.hst\ter1.atl\ter1.hst\tDefault\tDefault\t82.22
er1.atl_er1.lax\ter1.atl\ter1.lax\tDefault\tDefault\t153.19
er1.atl_er1.mia\ter1.atl\ter1.mia\tDefault\tDefault\t13.65
er1.atl_er1.nyc\ter1.atl\ter1.nyc\tDefault\tDefault\t207.86
er1.atl_er1.sea\ter1.atl\ter1.sea\tDefault\tDefault\t79.27
er1.atl_er1.sjc\ter1.atl\ter1.sjc\tDefault\tDefault\t168.8
er1.atl_er1.wdc\ter1.atl\ter1.wdc\tDefault\tDefault\t162.86
er1.bos_er1.atl\ter1.bos\ter1.atl\tDefault\tDefault\t34.0
er1.bos_er1.chi\ter1.bos\ter1.chi\tDefault\tDefault\t17.09
er1.bos_er1.hst\ter1.bos\ter1.hst\tDefault\tDefault\t36.0
er1.bos_er1.lax\ter1.bos\ter1.lax\tDefault\tDefault\t61.94
er1.bos_er1.mia\ter1.bos\ter1.mia\tDefault\tDefault\t14.05
er1.bos_er1.nyc\ter1.bos\ter1.nyc\tDefault\tDefault\t132.6
er1.bos_er1.sea\ter1.bos\ter1.sea\tDefault\tDefault\t32.0
er1.bos_er1.sjc\ter1.bos\ter1.sjc\tDefault\tDefault\t69.2
er1.bos_er1.wdc\ter1.bos\ter1.wdc\tDefault\tDefault\t69.33
er1.chi_er1.atl\ter1.chi\ter1.atl\tDefault\tDefault\t99.87
er1.chi_er1.bos\ter1.chi\ter1.bos\tDefault\tDefault\t39.4
er1.chi_er1.hst\ter1.chi\ter1.hst\tDefault\tDefault\t79.27
er1.chi_er1.lax\ter1.chi\ter1.lax\tDefault\tDefault\t160.05
er1.chi_er1.mia\ter1.chi\ter1.mia\tDefault\tDefault\t41.26
er1.chi_er1.nyc\ter1.chi\ter1.nyc\tDefault\tDefault\t150.16
er1.chi_er1.sea\ter1.chi\ter1.sea\tDefault\tDefault\t145.23
er1.chi_er1.sjc\ter1.chi\ter1.sjc\tDefault\tDefault\t178.81
er1.chi_er1.wdc\ter1.chi\ter1.wdc\tDefault\tDefault\t203.67
er1.hst_er1.atl\ter1.hst\ter1.atl\tDefault\tDefault\t124.28
er1.hst_er1.bos\ter1.hst\ter1.bos\tDefault\tDefault\t64.99
er1.hst_er1.chi\ter1.hst\ter1.chi\tDefault\tDefault\t97.81
er1.hst_er1.lax\ter1.hst\ter1.lax\tDefault\tDefault\t140.04
er1.hst_er1.mia\ter1.hst\ter1.mia\tDefault\tDefault\t38.51
er1.hst_er1.nyc\ter1.hst\ter1.nyc\tDefault\tDefault\t184.23
er1.hst_er1.sea\ter1.hst\ter1.sea\tDefault\tDefault\t90.41
er1.hst_er1.sjc\ter1.hst\ter1.sjc\tDefault\tDefault\t128.58
er1.hst_er1.wdc\ter1.hst\ter1.wdc\tDefault\tDefault\t144.35
er1.lax_er1.atl\ter1.lax\ter1.atl\tDefault\tDefault\t55.31
er1.lax_er1.bos\ter1.lax\ter1.bos\tDefault\tDefault\t74.32
er1.lax_er1.chi\ter1.lax\ter1.chi\tDefault\tDefault\t83.32
er1.lax_er1.hst\ter1.lax\ter1.hst\tDefault\tDefault\t126.02
er1.lax_er1.lax\ter1.lax\ter1.lax\tDefault\tDefault\t0.01
er1.lax_er1.mia\ter1.lax\ter1.mia\tDefault\tDefault\t36.98
er1.lax_er1.nyc\ter1.lax\ter1.nyc\tDefault\tDefault\t161.29
er1.lax_er1.sea\ter1.lax\ter1.sea\tDefault\tDefault\t88.32
er1.lax_er1.sjc\ter1.lax\ter1.sjc\tDefault\tDefault\t125.61
er1.lax_er1.wdc\ter1.lax\ter1.wdc\tDefault\tDefault\t157.26
er1.mia_er1.atl\ter1.mia\ter1.atl\tDefault\tDefault\t29.96
er1.mia_er1.bos\ter1.mia\ter1.bos\tDefault\tDefault\t28.25
er1.mia_er1.chi\ter1.mia\ter1.chi\tDefault\tDefault\t40.72
er1.mia_er1.hst\ter1.mia\ter1.hst\tDefault\tDefault\t55.91
er1.mia_er1.lax\ter1.mia\ter1.lax\tDefault\tDefault\t57.78
er1.mia_er1.mia\ter1.mia\ter1.mia\tDefault\tDefault\t6.28
er1.mia_er1.nyc\ter1.mia\ter1.nyc\tDefault\tDefault\t80.07
er1.mia_er1.sea\ter1.mia\ter1.sea\tDefault\tDefault\t30.53
er1.mia_er1.sjc\ter1.mia\ter1.sjc\tDefault\tDefault\t64.55
er1.mia_er1.wdc\ter1.mia\ter1.wdc\tDefault\tDefault\t72.83
er1.nyc_er1.atl\ter1.nyc\ter1.atl\tDefault\tDefault\t218.63
er1.nyc_er1.bos\ter1.nyc\ter1.bos\tDefault\tDefault\t300.82
er1.nyc_er1.chi\ter1.nyc\ter1.chi\tDefault\tDefault\t179.0
er1.nyc_er1.hst\ter1.nyc\ter1.hst\tDefault\tDefault\t231.53
er1.nyc_er1.lax\ter1.nyc\ter1.lax\tDefault\tDefault\t259.81
er1.nyc_er1.mia\ter1.nyc\ter1.mia\tDefault\tDefault\t90.33
er1.nyc_er1.sea\ter1.nyc\ter1.sea\tDefault\tDefault\t134.23
er1.nyc_er1.sjc\ter1.nyc\ter1.sjc\tDefault\tDefault\t290.27
er1.nyc_er1.wdc\ter1.nyc\ter1.wdc\tDefault\tDefault\t445.87
er1.sea_er1.atl\ter1.sea\ter1.atl\tDefault\tDefault\t90.07
er1.sea_er1.bos\ter1.sea\ter1.bos\tDefault\tDefault\t62.4
er1.sea_er1.chi\ter1.sea\ter1.chi\tDefault\tDefault\t174.66
er1.sea_er1.hst\ter1.sea\ter1.hst\tDefault\tDefault\t90.4
er1.sea_er1.lax\ter1.sea\ter1.lax\tDefault\tDefault\t109.21
er1.sea_er1.mia\ter1.sea\ter1.mia\tDefault\tDefault\t37.21
er1.sea_er1.nyc\ter1.sea\ter1.nyc\tDefault\tDefault\t135.43
er1.sea_er1.sea\ter1.sea\ter1.sea\tDefault\tDefault\t45.82
er1.sea_er1.sjc\ter1.sea\ter1.sjc\tDefault\tDefault\t140.15
er1.sea_er1.wdc\ter1.sea\ter1.wdc\tDefault\tDefault\t183.69
er1.sjc_er1.atl\ter1.sjc\ter1.atl\tDefault\tDefault\t248.66
er1.sjc_er1.bos\ter1.sjc\ter1.bos\tDefault\tDefault\t204.96
er1.sjc_er1.chi\ter1.sjc\ter1.chi\tDefault\tDefault\t229.79
er1.sjc_er1.hst\ter1.sjc\ter1.hst\tDefault\tDefault\t210.2
er1.sjc_er1.lax\ter1.sjc\ter1.lax\tDefault\tDefault\t253.96
er1.sjc_er1.mia\ter1.sjc\ter1.mia\tDefault\tDefault\t102.0
er1.sjc_er1.nyc\ter1.sjc\ter1.nyc\tDefault\tDefault\t444.8
er1.sjc_er1.sea\ter1.sjc\ter1.sea\tDefault\tDefault\t229.16
er1.sjc_er1.sjc\ter1.sjc\ter1.sjc\tDefault\tDefault\t0.01
er1.sjc_er1.wdc\ter1.sjc\ter1.wdc\tDefault\tDefault\t433.69
er1.wdc_er1.atl\ter1.wdc\ter1.atl\tDefault\tDefault\t183.09
er1.wdc_er1.bos\ter1.wdc\ter1.bos\tDefault\tDefault\t194.18
er1.wdc_er1.chi\ter1.wdc\ter1.chi\tDefault\tDefault\t279.92
er1.wdc_er1.hst\ter1.wdc\ter1.hst\tDefault\tDefault\t193.89
er1.wdc_er1.lax\ter1.wdc\ter1.lax\tDefault\tDefault\t296.25
er1.wdc_er1.mia\ter1.wdc\ter1.mia\tDefault\tDefault\t101.43
er1.wdc_er1.nyc\ter1.wdc\ter1.nyc\tDefault\tDefault\t550.44
er1.wdc_er1.sea\ter1.wdc\ter1.sea\tDefault\tDefault\t209.91
er1.wdc_er1.sjc\ter1.wdc\ter1.sjc\tDefault\tDefault\t330.97
er1.wdc_er1.wdc\ter1.wdc\ter1.wdc\tDefault\tDefault\t71.79

<NodeTraffic>
Node\tTrafficLevel\tSrcTraffMeas\tDestTraffMeas
cr1.atl\tDefault\t0.0\t0.0
cr1.bos\tDefault\t0.0\t0.0
cr1.chi\tDefault\t0.0\t0.0
cr1.hst\tDefault\t0.0\t0.0
cr1.kcy\tDefault\t0.0\t0.0
cr1.lax\tDefault\t0.0\t0.0
cr1.mia\tDefault\t0.0\t0.0
cr1.nyc\tDefault\t0.0\t0.0
cr1.sea\tDefault\t0.0\t0.0
cr1.sjc\tDefault\t0.0\t0.0
cr1.wdc\tDefault\t0.0\t0.0
cr2.atl\tDefault\t0.0\t0.0
cr2.bos\tDefault\t0.0\t0.0
cr2.chi\tDefault\t0.0\t0.0
cr2.hst\tDefault\t0.0\t0.0
cr2.kcy\tDefault\t0.0\t0.0
cr2.lax\tDefault\t0.0\t0.0
cr2.mia\tDefault\t0.0\t0.0
cr2.nyc\tDefault\t0.0\t0.0
cr2.sea\tDefault\t0.0\t0.0
cr2.sjc\tDefault\t0.0\t0.0
cr2.wdc\tDefault\t0.0\t0.0
er1.atl\tDefault\t1046.89\t1083.85
er1.bos\tDefault\t466.193\t1042.65
er1.chi\tDefault\t1097.73\t1208.01
er1.hst\tDefault\t1013.2\t1105.45
er1.kcy\tDefault\t0.0\t0.0
er1.lax\tDefault\t896.167\t1492.24
er1.mia\tDefault\t466.868\t481.692
er1.nyc\tDefault\t2150.49\t2046.87
er1.sea\tDefault\t1069.03\t1064.28
er1.sjc\tDefault\t2357.22\t1496.95
er1.wdc\tDefault\t2411.86\t1873.54

<InterfaceTraffic>
Node\tInterface\tTrafficLevel\tTraffMeas
cr1.atl\tto_cr1.hst\tDefault\t823.48
cr1.atl\tto_cr2.atl\tDefault\t442.89
cr1.atl\tto_er1.atl\tDefault\t209.55
cr1.bos\tto_cr2.bos\tDefault\t0.0
cr1.bos\tto_cr2.chi\tDefault\t272.19
cr1.bos\tto_er1.bos\tDefault\t381.08
cr1.chi\tto_cr2.chi\tDefault\t583.13
cr1.chi\tto_cr2.nyc\tDefault\t1586.0
cr1.chi\tto_cr2.sea\tDefault\t753.23
cr1.chi\tto_er1.chi\tDefault\t780.0
cr1.hst\tto_cr1.atl\tDefault\t528.75
cr1.hst\tto_cr2.hst\tDefault\t132.39
cr1.hst\tto_cr2.kcy\tDefault\t86.197
cr1.hst\tto_er1.hst\tDefault\t678.83
cr1.kcy\tto_cr1.sjc\tDefault\t1023.1
cr1.kcy\tto_cr1.wdc\tDefault\t978.58
cr1.kcy\tto_cr2.kcy\tDefault\t0.0
cr1.kcy\tto_er1.kcy\tDefault\t0.0
cr1.lax\tto_cr2.hst\tDefault\t481.93
cr1.lax\tto_cr2.lax\tDefault\t218.99
cr1.lax\tto_er1.lax\tDefault\t293.23
cr1.mia\tto_cr2.mia\tDefault\t0.0
cr1.mia\tto_cr2.wdc\tDefault\t396.99
cr1.mia\tto_er1.mia\tDefault\t400.98
cr1.nyc\tto_cr2.bos\tDefault\t647.46
cr1.nyc\tto_cr2.nyc\tDefault\t746.04
cr1.nyc\tto_cr2.wdc\tDefault\t1795.5
cr1.nyc\tto_er1.nyc\tDefault\t1155.2
cr1.sea\tto_cr2.sea\tDefault\t0.0
cr1.sea\tto_cr2.sjc\tDefault\t306.91
cr1.sea\tto_er1.sea\tDefault\t433.11
cr1.sjc\tto_cr1.kcy\tDefault\t944.4
cr1.sjc\tto_cr2.chi\tDefault\t909.95
cr1.sjc\tto_cr2.sjc\tDefault\t835.83
cr1.sjc\tto_er1.sjc\tDefault\t1102.6
cr1.wdc\tto_cr1.kcy\tDefault\t834.68
cr1.wdc\tto_cr2.atl\tDefault\t1283.5
cr1.wdc\tto_cr2.wdc\tDefault\t854.39
cr1.wdc\tto_er1.wdc\tDefault\t898.15
cr2.atl\tto_cr1.atl\tDefault\t407.0
cr2.atl\tto_cr1.wdc\tDefault\t1191.4
cr2.atl\tto_er1.atl\tDefault\t874.3
cr2.bos\tto_cr1.bos\tDefault\t0.0
cr2.bos\tto_cr1.nyc\tDefault\t360.8
cr2.bos\tto_er1.bos\tDefault\t661.57
cr2.chi\tto_cr1.bos\tDefault\t426.04
cr2.chi\tto_cr1.chi\tDefault\t716.92
cr2.chi\tto_cr1.sjc\tDefault\t1161.6
cr2.chi\tto_cr2.kcy\tDefault\t79.272
cr2.chi\tto_er1.chi\tDefault\t428.01
cr2.hst\tto_cr1.hst\tDefault\t72.12
cr2.hst\tto_cr1.lax\tDefault\t532.0
cr2.hst\tto_er1.hst\tDefault\t426.62
cr2.kcy\tto_cr1.hst\tDefault\t70.001
cr2.kcy\tto_cr1.kcy\tDefault\t0.0
cr2.kcy\tto_cr2.chi\tDefault\t102.82
cr2.kcy\tto_er1.kcy\tDefault\t0.0
cr2.lax\tto_cr1.lax\tDefault\t300.58
cr2.lax\tto_cr2.sjc\tDefault\t1047.7
cr2.lax\tto_er1.lax\tDefault\t1199.0
cr2.mia\tto_cr1.atl\tDefault\t82.519
cr2.mia\tto_cr1.mia\tDefault\t0.0
cr2.mia\tto_er1.mia\tDefault\t80.707
cr2.nyc\tto_cr1.chi\tDefault\t1631.3
cr2.nyc\tto_cr1.nyc\tDefault\t639.22
cr2.nyc\tto_er1.nyc\tDefault\t891.68
cr2.sea\tto_cr1.chi\tDefault\t676.94
cr2.sea\tto_cr1.sea\tDefault\t0.0
cr2.sea\tto_er1.sea\tDefault\t631.17
cr2.sjc\tto_cr1.sea\tDefault\t407.89
cr2.sjc\tto_cr1.sjc\tDefault\t652.03
cr2.sjc\tto_cr2.lax\tDefault\t1443.8
cr2.sjc\tto_er1.sjc\tDefault\t394.34
cr2.wdc\tto_cr1.mia\tDefault\t423.26
cr2.wdc\tto_cr1.nyc\tDefault\t1898.1
cr2.wdc\tto_cr1.wdc\tDefault\t730.51
cr2.wdc\tto_er1.wdc\tDefault\t975.39
er1.atl\tto_cr1.atl\tDefault\t249.06
er1.atl\tto_cr2.atl\tDefault\t797.83
er1.bos\tto_cr1.bos\tDefault\t180.23
er1.bos\tto_cr2.bos\tDefault\t285.97
er1.chi\tto_cr1.chi\tDefault\t640.2
er1.chi\tto_cr2.chi\tDefault\t457.53
er1.hst\tto_cr1.hst\tDefault\t654.17
er1.hst\tto_cr2.hst\tDefault\t359.03
er1.kcy\tto_cr1.kcy\tDefault\t0.0
er1.kcy\tto_cr2.kcy\tDefault\t0.0
er1.lax\tto_cr1.lax\tDefault\t169.07
er1.lax\tto_cr2.lax\tDefault\t727.1
er1.mia\tto_cr1.mia\tDefault\t381.0
er1.mia\tto_cr2.mia\tDefault\t85.867
er1.nyc\tto_cr1.nyc\tDefault\t1287.2
er1.nyc\tto_cr2.nyc\tDefault\t863.31
er1.sea\tto_cr1.sea\tDefault\t339.76
er1.sea\tto_cr2.sea\tDefault\t729.27
er1.sjc\tto_cr1.sjc\tDefault\t1663.9
er1.sjc\tto_cr2.sjc\tDefault\t693.32
er1.wdc\tto_cr1.wdc\tDefault\t1004.2
er1.wdc\tto_cr2.wdc\tDefault\t1407.7

<PlotLayouts>
Name\tPlotLayoutType
Default\tDesign

<PlotLayoutElements>
PlotLayoutName\tElementSite\tElementNode\tX\tY\tShown
Default\tatl\t\t1200.0\t550.0\tT
Default\tbos\t\t1500.0\t50.0\tT
Default\tchi\t\t900.0\t50.0\tT
Default\thst\t\t900.0\t650.0\tT
Default\tkcy\t\t900.0\t350.0\tT
Default\tlax\t\t100.0\t550.0\tT
Default\tmia\t\t1400.0\t750.0\tT
Default\tnyc\t\t1450.0\t200.0\tT
Default\tsea\t\t50.0\t50.0\tT
Default\tsjc\t\t50.0\t350.0\tT
Default\twdc\t\t1400.0\t350.0\tT
Default\t\tcr1.atl\t1192.75\t546.5\tT
Default\t\tcr2.atl\t1207.25\t546.5\tT
Default\t\ter1.atl\t1200.0\t553.5\tT
Default\t\tcr1.bos\t1492.75\t46.5\tT
Default\t\tcr2.bos\t1507.25\t46.5\tT
Default\t\ter1.bos\t1500.0\t53.5\tT
Default\t\tcr1.chi\t892.75\t46.5\tT
Default\t\tcr2.chi\t907.25\t46.5\tT
Default\t\ter1.chi\t900.0\t53.5\tT
Default\t\tcr1.hst\t892.75\t646.5\tT
Default\t\tcr2.hst\t907.25\t646.5\tT
Default\t\ter1.hst\t900.0\t653.5\tT
Default\t\tcr1.kcy\t892.75\t346.5\tT
Default\t\tcr2.kcy\t907.25\t346.5\tT
Default\t\ter1.kcy\t900.0\t353.5\tT
Default\t\tcr1.lax\t92.75\t546.5\tT
Default\t\tcr2.lax\t107.25\t546.5\tT
Default\t\ter1.lax\t100.0\t553.5\tT
Default\t\tcr1.mia\t1392.75\t746.5\tT
Default\t\tcr2.mia\t1407.25\t746.5\tT
Default\t\ter1.mia\t1400.0\t753.5\tT
Default\t\tcr1.nyc\t1442.75\t196.5\tT
Default\t\tcr2.nyc\t1457.25\t196.5\tT
Default\t\ter1.nyc\t1450.0\t203.5\tT
Default\t\tcr1.sea\t42.75\t46.5\tT
Default\t\tcr2.sea\t57.25\t46.5\tT
Default\t\ter1.sea\t50.0\t53.5\tT
Default\t\tcr1.sjc\t42.75\t346.5\tT
Default\t\tcr2.sjc\t57.25\t346.5\tT
Default\t\ter1.sjc\t50.0\t353.5\tT
Default\t\tcr1.wdc\t1392.75\t346.5\tT
Default\t\tcr2.wdc\t1407.25\t346.5\tT
Default\t\ter1.wdc\t1400.0\t353.5\tT
"""

plan_content = b""
inventory_nodes = []
active_tgts = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def issue_jwt(username: str) -> str:
    """Return an HS256-signed JWT that cw_get_jwt.py --decode can inspect."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": username,
        "iss": "fake-cnc-server",
        "iat": now,
        "exp": now + JWT_LIFETIME_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    signature = hmac.new(JWT_SECRET, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def validate_jwt(token: str) -> bool:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        expected = hmac.new(JWT_SECRET, f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
            return False
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError):
        return False
    return payload.get("exp", 0) > time.time()


def parse_plan_table(content: bytes, table: str) -> list:
    """Return the rows of a text plan table as dicts keyed by column header."""
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []

    rows = []
    in_table = False
    header = None
    for line in lines:
        if line.strip() == f"<{table}>":
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("<"):
            break
        if not line.strip():
            continue
        columns = line.split("\t")
        if header is None:
            header = columns
            continue
        rows.append(dict(zip(header, columns)))
    return rows


def _coordinates(row: dict):
    """Return (longitude, latitude) floats from a plan row, or None if either is missing."""
    try:
        return float(row["Longitude"]), float(row["Latitude"])
    except (KeyError, ValueError):
        return None


def _hashed_coordinates(name: str) -> tuple:
    digest = hashlib.sha256(name.encode()).digest()
    longitude = round(int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 360 - 180, 6)
    latitude = round(int.from_bytes(digest[4:8], "big") / 0xFFFFFFFF * 180 - 90, 6)
    return longitude, latitude


def build_inventory(content: bytes) -> list:
    """Build DLM inventory records for every node in the plan's <Nodes> table.

    Coordinates come from the node's own Longitude/Latitude, then its <Sites>
    entry, then a deterministic hash of the node name.
    """
    site_coordinates = {}
    for site in parse_plan_table(content, "Sites"):
        coordinates = _coordinates(site)
        if site.get("Name") and coordinates:
            site_coordinates[site["Name"]] = coordinates

    nodes = []
    sources = {"node": 0, "site": 0, "hash": 0}
    for row in parse_plan_table(content, "Nodes"):
        name = row.get("Name")
        if not name:
            continue
        coordinates = _coordinates(row)
        source = "node"
        if coordinates is None:
            coordinates = site_coordinates.get(row.get("Site", ""))
            source = "site"
        if coordinates is None:
            coordinates = _hashed_coordinates(name)
            source = "hash"
        sources[source] += 1
        longitude, latitude = coordinates
        nodes.append({
            "host_name": name,
            "geo_info": {
                "coordinates": {
                    "longitude": {"value": longitude},
                    "latitude": {"value": latitude},
                }
            },
        })
    print(f"[CNC] Node coordinates from: {sources['node']} node, "
          f"{sources['site']} site, {sources['hash']} generated")
    return nodes


def load_plan_content(plan_file: str) -> None:
    global plan_content, inventory_nodes

    if plan_file:
        print(f"[CNC] Loading plan from: {plan_file}")
        with open(plan_file, "rb") as f:
            plan_content = f.read()
    else:
        print("[CNC] Using embedded US WAN sample plan")
        plan_content = DEFAULT_PLAN.encode()

    inventory_nodes = build_inventory(plan_content)
    print(f"[CNC] DLM inventory contains {len(inventory_nodes)} nodes")


class MockCNCHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[CNC] {self.address_string()} {format % args}")

    def send_body(self, body: bytes, content_type: str, status: int = 200, headers: dict = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status: int = 200, content_type: str = "application/json"):
        self.send_body(json.dumps(data).encode(), content_type, status)

    def send_text(self, text: str, status: int = 200, headers: dict = None):
        self.send_body(text.encode(), "text/plain", status, headers)

    def read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode() if length else ""

    def read_form(self) -> dict:
        return {key: values[0] for key, values in parse_qs(self.read_body()).items()}

    def read_json(self) -> dict:
        try:
            data = json.loads(self.read_body() or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and validate_jwt(auth[len("Bearer "):].strip()):
            return True
        self.send_json({"error": "Unauthorized"}, 401)
        return False

    def do_POST(self):
        path = urlparse(self.path).path

        if path == SSO_TICKETS_PATH:
            self.handle_ticket()
        elif path == SSO_JWT_PATH:
            self.handle_jwt(self.read_form().get("tgt", ""))
        elif path.startswith(f"{SSO_TICKETS_PATH}/TGT-"):
            self.handle_jwt(path.rsplit("/", 1)[-1])
        elif path == GET_PLAN_PATH:
            self.handle_get_plan()
        elif path == INVENTORY_QUERY_PATH:
            self.handle_inventory_query()
        else:
            self.send_text("Not Found", 404)

    def handle_ticket(self):
        form = self.read_form()
        username = form.get("username", "")
        if not username or not form.get("password"):
            self.send_text("Invalid credentials", 401)
            return
        tgt = f"TGT-{uuid.uuid4().hex}"
        active_tgts[tgt] = username
        print(f"[CNC] Issued TGT for user: {username}")
        location = f"https://{self.headers.get('Host', 'localhost')}{SSO_TICKETS_PATH}/{tgt}"
        self.send_text(tgt, 201, {"Location": location})

    def handle_jwt(self, tgt: str):
        username = active_tgts.get(tgt)
        if username is None:
            self.send_text("Invalid TGT", 401)
            return
        print(f"[CNC] Issued JWT for user: {username}")
        self.send_text(issue_jwt(username))

    def handle_get_plan(self):
        if not self.authorized():
            return
        input_data = self.read_json().get("input", {})
        file_format = input_data.get("format", "txt")
        version = input_data.get("version", "")
        print(f"[CNC] Plan request - format: {file_format}, version: {version!r}")

        response = {
            "cisco-crosswork-optimization-engine-operations:output": {
                "planfile-content": base64.b64encode(plan_content).decode()
            }
        }
        self.send_json(response, content_type="application/yang-data+json")

    def handle_inventory_query(self):
        if not self.authorized():
            return
        print(f"[CNC] Inventory query - returning {len(inventory_nodes)} nodes")
        self.send_json({"data": inventory_nodes})


def create_ssl_context() -> ssl.SSLContext:
    """Create a TLS context with a throwaway self-signed certificate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_file = os.path.join(tmpdir, "cert.pem")
        key_file = os.path.join(tmpdir, "key.pem")
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_file, "-out", cert_file,
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
        ], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
    return context


def main():
    parser = argparse.ArgumentParser(description="Mock CNC Server for testing get_plan.py")
    parser.add_argument("--port", type=int, default=30603, help="Port to listen on (default: 30603)")
    parser.add_argument("--no-ssl", action="store_true",
                        help="Disable SSL (use HTTP instead of HTTPS). get_plan.py always uses HTTPS.")
    parser.add_argument("--plan-file",
                        help="Path to plan file to serve (default: embedded US WAN sample plan)")
    args = parser.parse_args()

    if args.plan_file and not os.path.isfile(args.plan_file):
        parser.error(f"plan file not found: {args.plan_file}")

    sys.stdout.reconfigure(line_buffering=True)
    load_plan_content(args.plan_file)

    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), MockCNCHandler)

    if args.no_ssl:
        protocol = "HTTP"
    else:
        print("[CNC] Generating self-signed certificate...")
        httpd.socket = create_ssl_context().wrap_socket(httpd.socket, server_side=True)
        protocol = "HTTPS"

    print(f"[CNC] Mock CNC Server started on {protocol}://0.0.0.0:{args.port}")
    port_arg = "" if args.port == 30603 else f" --port {args.port}"
    print(f"[CNC] Test with: python get_plan.py --ip 127.0.0.1{port_arg} -u admin -p admin -k -f plan.txt")
    print("[CNC] Press Ctrl+C to stop")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[CNC] Server stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
