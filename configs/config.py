# Time format processing arguments and naming files
time_fmt = '%Y%m%d.%H%M'

# Data collection data range with inclusive borders
date_range = "20100901.0000,20100901.0200"

# Most of files will use this prefix
# Will make a folder in output_dir
output_filename = "default_conf"

# Time between observers dump in seconds
interval = 900

# Collector names
collectors = ["route-views.sydney", "route-views.wide"]

# Routing table for only ASNs in this list
peer_asns = """"""

# Routing table for only peers in this list
peer_ips = """"""

peer_asns = peer_asns.splitlines()
peer_ips = peer_ips.splitlines()