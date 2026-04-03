"""Copy IPv6 IGP/TE metrics to IP metrics in a plan file

Used as a Crosswork Planning external script. The Collector framework
passes positional arguments: base planfile path, output planfile path,
and possibly extra args (ignored).

For reference please see:
https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts

"""

import sys
from com.cisco.wae.opm.network import Network

def copy_ipv6_metrics(src, dest):
    """Read a planfile, copy IPv6 metrics to IP metrics, and write the result."""
    network = Network(src)
    copy_count = 0

    for node in network.model.nodes:
        for interface in node.interfaces:
            """rpc_record is used for v6 metrics since this is not exposed in OPM on node.interfaces"""
            rec = interface.rpc_record
            v6igp = rec.ipv6IGPMetric
            v6te = rec.ipv6TEMetric

            if isinstance(v6igp, int) and v6igp != interface.igp_metric:
                print("Copying IPv6 IGP Metric for {} {}: {} -> {}".format(node.name, interface.name, v6igp, interface.igp_metric))
                interface.igp_metric = v6igp
                copy_count += 1

            if isinstance(v6te, int) and v6te != interface.te_metric:
                print("Copying IPv6 TE Metric for {} {}: {} -> {}".format(node.name, interface.name, v6te, interface.te_metric))
                interface.te_metric = v6te
                copy_count += 1

    network.write(dest)
    print("Total number of interface metrics updated: {}".format(copy_count))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: {} <base_planfile> <output_planfile> [extra args...]".format(sys.argv[0]))
        sys.exit(1)

    base_planfile = sys.argv[1]
    output_planfile = sys.argv[2]
    # Extra positional args from the Collector framework are ignored.

    copy_ipv6_metrics(base_planfile, output_planfile)
