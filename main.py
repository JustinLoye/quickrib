"""
Main program.

Input: data collection config (default: configs/config.py,
can be overridden by command line argument or mounting user config file with docker run)

Output: observer dumps in ./data (can be overriden by mounting with docker run)
"""

import os
import argparse
from datetime import datetime, timedelta
from url_generation import RIS_url, RV_url, RV_UPDATE_RES, RIS_UPDATE_RES
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import logging
import sys
import cProfile
from math import floor
from requests_cache import CachedSession
from rib_table import RIBTable
from observers.graph import ASGraphObserver, ASMultiGraphObserver
from observers.update_count import UpdateCountObserver
from observers.path import PathObserver
import configs.config as config

OUTPUT_DIR = "./data"


class BGPDownloader:
    """
    BGPDownloader is the main class designed for orchestrating the fine-grain RIB reconstruction from updates.
    It includes functionalities for defining the collection parameter,
    downloading and caching files from RouteViews (RV) and Routing Information Service (RIS), 
    setting the RIBTable observers, performing reconstructed RIB comparaison to ground-truth.

    Attributes
    ----------
    output_dir : str
        Main folder where data will be dumped to.
    output_filename : str
        Filename prefix and folder name to be created in `output_dir`.
    collectors : list
        List of route collectors to get data from.
    peer_asns : list
        List of peer asns to get data from. Default is `[]` for all peer asns.
    peer_ips : list
        List of peer ips to get data from. Default is `[]` for all peer ips.
    interval : int
        interval time to dump reconstructed rib and/or observers.
    session : CachedSession
        requests-cache session giving access to input RIB and updates files.
    time_fmt : str
        Time format in output files.
    files : pandas.DataFrame
        Multi-index `[timestamp, route_collector, file_type]` dataframe the keep tracks of the files to download.
    ts_start : datetime
        Data processing start time.
    ts_end : datetime
        Data processing end time.
    compare : Boolean
        Determine if ts_end is suitable to build a RIB table with all collectors, which is used to quantify the RIB reconstruction error. 
     
    Methods
    -------
    set_urls() -> None
        Set the RV and RIS file urls to download.
    download_urls() -> None
        Download and cache the aforementioned urls.
    warm_update_process() -> None
        Main function that builds a RIB table (and observers) and then apply updates to reconstruct subsequent RIBs.
    """
    
    def __init__(self, output_dir: str, output_filename: str, date_range: str, collectors: list, peer_asns: list, peer_ips: list, interval: int, session: CachedSession, time_fmt: str):
        """
        Set up data collection.

        Parameters
        ----------
        output_dir : str
            Main folder where data will be dumped to.
        output_filename : str
            Filename prefix and folder name to be created in `output_dir`.
        collectors : list
            List of route collectors to get data from.
        peer_asns : list
            List of peer asns to get data from. Default is `[]` for all peer asns.
        peer_ips : list
            List of peer ips to get data from. Default is `[]` for all peer ips.
        interval : int
            interval time to dump reconstructed rib and/or observers.
        session : CachedSession
            requests-cache session giving access to input RIB and updates files.
        time_fmt : str
            time format in output files.
        """
        self.output_dir = output_dir
        self.output_filename = output_filename
        self.collectors = collectors
        self.peer_asns = [int(peer_asn) for peer_asn in peer_asns]
        self.peer_ips = peer_ips
        self.interval = interval
        self.session = session
        self.time_fmt = time_fmt
        
        # Processing the arguments
        self.ts_start = datetime.strptime(
            date_range.split(",")[0], self.time_fmt)
        self.ts_end = datetime.strptime(
            date_range.split(",")[1], self.time_fmt)

        self.processed_dir = os.path.join(self.output_dir, 'processed')
        os.makedirs(self.processed_dir, exist_ok=True)
        
        self.projects = set()
        for collector in collectors:
            if collector[:3] == "rrc":
                self.projects.add("RIS")
            elif "route-views" in collector:
                self.projects.add("RV")
            else:
                raise ValueError(f"rc {collector} not recognized")
        
        # Boolean to perform reconstruction error checks. Will be set to true if ts_end is a rib time.
        self.compare = False
        
    def set_urls(self):
        """Given the time interval and RCs, init a df that keep track of the files url to download"""

        # Get the closest_rib to the start
        if "RIS" in self.projects:
            closest_ribs = [datetime(self.ts_start.year, self.ts_start.month, self.ts_start.day) -
                            timedelta(days=1) + timedelta(hours=i*8) for i in range(10)]
        else:
            closest_ribs = [datetime(self.ts_start.year, self.ts_start.month, self.ts_start.day) -
                            timedelta(days=1) + timedelta(hours=i*2) for i in range(50)]
        closest_rib = closest_ribs[np.argmin(
            [abs((candidate - self.ts_start).total_seconds()) for candidate in closest_ribs])]
        self.ts_start = closest_rib
        logging.info(
            f"Setting start of time interval to {self.ts_start.strftime('%Y%m%d.%H%M')}")

        # Get closest update to end (for simplicity, common to both RIS and RV ---> RV resolution since it's the worst)
        closest_updates = [datetime(self.ts_end.year, self.ts_end.month, self.ts_end.day, self.ts_end.hour) -
                           timedelta(hours=1) + timedelta(seconds=i*RV_UPDATE_RES) for i in range(10)]
        closest_update = closest_updates[np.argmin(
            [abs((candidate - self.ts_end).total_seconds()) for candidate in closest_updates])]
        self.ts_end = closest_update
        logging.info(
            f"Setting end of time interval to {self.ts_end.strftime('%Y%m%d.%H%M')}")

        # Get the RIS updates datetimes
        # I download more updates before and after the interval just in case
        updates_number = floor(
            (self.ts_end - self.ts_start).total_seconds()/RIS_UPDATE_RES) + 1
        ris_updates_dts = [self.ts_start + timedelta(seconds=i*RIS_UPDATE_RES)
                           for i in range(-1, updates_number+2)]

        # Get the RV updates datetimes
        updates_number = floor(
            (self.ts_end - self.ts_start).total_seconds()/RV_UPDATE_RES) + 1
        rv_updates_dts = [self.ts_start + timedelta(seconds=i*RV_UPDATE_RES)
                          for i in range(-1, updates_number+2)]

        # Keep track of the input RIB + updates files
        multi_index = pd.MultiIndex.from_product([ris_updates_dts, self.collectors, [
            "rib", "update"]], names=['datetime', 'RC', 'type'])
        self.files = pd.DataFrame(columns=['urls'], index=multi_index)
        for rc in self.collectors:
            if rc.startswith("rrc"):
                
                # Get RIS initial RIB files
                url = RIS_url(rc, self.ts_start, "rib")
                self.files.loc[(self.ts_start, rc, "rib"), "urls"] = url

                # Get RIS updates files
                for dt in ris_updates_dts:
                    url = RIS_url(rc, dt, "update")
                    self.files.loc[(dt, rc, "update"), "urls"] = url
                    
                # If ts_end is a RIB time, get ground truth RIS RIB to check reconstruction errors
                if self.ts_end.minute == 0:
                    if self.ts_end.hour % 8 == 0:
                        self.compare = True
                        url = RIS_url(rc, self.ts_end, "rib")
                        self.files.loc[(self.ts_end, rc,
                                        "rib"), "urls"] = url

            if rc.startswith("route-views"):
                
                # Get RV initial RIB files
                url = RV_url(rc, self.ts_start, "rib")
                self.files.loc[(self.ts_start, rc, "rib"), "urls"] = url

                # Get RV updates files
                for dt in rv_updates_dts:
                    url = RV_url(rc, dt, "update")
                    self.files.loc[(dt, rc, "update"), "urls"] = url

                # If ts_end is a RIB time, get ground truth RV RIB to check reconstruction errors
                if self.ts_end.minute == 0:
                    if self.ts_end.hour % 2 == 0:
                        if len(self.projects) == 1:
                                self.compare = True
                                url = RV_url(rc, self.ts_end, "rib")
                                self.files.loc[(self.ts_end, rc, "rib"), "urls"] = url
                        elif len(self.projects) == 2:
                            if self.ts_end % 8 == 0:
                                self.compare = True
                                url = RV_url(rc, self.ts_end, "rib")
                                self.files.loc[(self.ts_end, rc,
                                                "rib"), "urls"] = url
        
        if self.compare:                    
            logging.info(
                "ts_end is a RIB time. Reconstruction error will be assessed.")
        else:
            logging.info(
                "ts_end is not a RIB time. Reconstruction error will not be assessed.")
        
        self.files = self.files.dropna().sort_index()
        

    def download_urls(self):
        
        urls = list(self.files["urls"].values)
            
        # Cache stats for n3rdZ
        from_cache_size = 0
        total_size = 0
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            
            # Launch concurrent downloads
            future_to_url = {
                executor.submit(
                    self.session.get,
                    url,
                ): url for url in urls}
            
            # Async results collection
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                response = future.result()
                if response.status_code != 200:
                    raise RuntimeError(f"Error fetching {url}")
                content_size = len(response.content)
                if response.from_cache:
                    from_cache_size += content_size
                    total_size += content_size
                else:
                    total_size += content_size
        
        logging.info(f"Collected {from_cache_size / 1e9: .3f} GB from cache out of {total_size / 1e9: .3f} GB")

    def warm_update_process(self):
        """Main function. Warm start with RIB and then apply updates. Periodically dump observers. At the end compare reconstruction to ground_truth if ts_end is a RIB time"""
        
        # Determining the output file timestamps
        # They should overlap with update times
        output_files_number = int((self.ts_end - self.ts_start).total_seconds()/self.interval)
        self.output_files = [self.ts_start + timedelta(seconds=i*self.interval)
                            for i in range(1, output_files_number+1)]
        
        # Processing the initial RIBs
        rib_table = RIBTable(
            session=self.session,
            peer_ip_filter=self.peer_ips,
            ts_start=self.ts_start,
            ts_end=self.ts_end,
        )
        
        observers_output_dir = os.path.join(self.processed_dir, self.output_filename)
        os.makedirs(observers_output_dir, exist_ok=True)
        
        # Select observers. Feel free to add yours.
        as_multigraph_observer = ASMultiGraphObserver(name="multigraph",
                                                      output_dir=observers_output_dir)
        as_graph_observer = ASGraphObserver(name="graph",
                                            output_dir=observers_output_dir,
                                            multigraph_observer=as_multigraph_observer)
        update_count_observer = UpdateCountObserver(name="update_count",
                                                          output_dir=observers_output_dir)
        path_observer = PathObserver(name="path", output_dir=observers_output_dir)
        
        # Attach observer
        rib_table.attach_observer(as_graph_observer)
        rib_table.attach_observer(as_multigraph_observer)
        rib_table.attach_observer(update_count_observer)
        rib_table.attach_observer(path_observer)
        
        # Get urls to initial RIBs (warm start at ts_start) 
        rc_to_url = {}
        for rc in self.collectors:
            rc_to_url[rc] = self.files.loc[(self.ts_start, rc, "rib"), "urls"]
        
        # Process the RIBS
        rib_table.build(rc_to_url)
        
        rib_table.dump(self.ts_start)
        logging.info(f"Dump observers at {self.ts_start}")
        
        # Get the timestamps of updates
        ris_timestamps = (self.files.loc[(slice(None), slice(None), "update")]
                          .index.get_level_values("datetime").unique().tolist())
        
        # For each timestamp...
        for ts in ris_timestamps:
            logging.info(f"Processing updates at timestamp {ts}")
            
            # Get the collectors having updates
            collectors_for_timestamp = (self.files.loc[(ts, slice(None), "update"), :]
                                        .index.get_level_values('RC').tolist())
            logging.info(f"Collectors available for timestamp: {collectors_for_timestamp}")
            
            # Get the update files
            rc_to_url = {}
            for rc in collectors_for_timestamp:
                rc_to_url[rc] = self.files.loc[(ts, rc, "update"), "urls"]
            
            # Process the updates
            rib_table.update(rc_to_url)
                                 
            # Dump the observers
            if ts in self.output_files:
                rib_table.dump(ts)
                logging.info(f"Dump observers at {ts}")
            
            # print(rib_table.stop_updating)
            if all(rib_table.stop_updating.values()):
                break
            
        if not self.compare:
            return
        
        # Perform checks if needed
        # Get urls to end RIBs
        rc_to_url = {}
        for rc in self.collectors:
            rc_to_url[rc] = self.files.loc[(self.ts_end, rc, "rib"), "urls"]
            
        # Processing the final RIBs
        end_rib_table = RIBTable(
            session=self.session,
            peer_ip_filter=self.peer_ips,
            ts_start=self.ts_start,
            ts_end=self.ts_end
        )

        # Select observers (name must match already defined observers)
        end_as_graph_observer = ASGraphObserver("graph")
        
        # Attach observers to check
        end_rib_table.attach_observer(end_as_graph_observer)
        
        # Process the RIB
        end_rib_table.build(rc_to_url)
        
        # Compare RIB and its observers to ground-truth
        rib_table.compare(end_rib_table)
                        
    def run(self):
        
        self.set_urls()        
        self.download_urls()
        self.warm_update_process()

if __name__ == "__main__":
    
    # Create an argument parser
    parser = argparse.ArgumentParser(description="BGP downloader")

    parser.add_argument('--output_filename', '-f', type=str, default=config.output_filename,
                        help="output files directory and base name (time and extension will be appended)")
    parser.add_argument('--date_range', '-d', type=str, default=config.date_range,
                        help="<start>[,<end>] process records within the given time window (end is inclusive) default %%Y%%m%%d.%%H%%M format")
    parser.add_argument('--collector', '-c', default=config.collectors, nargs='+',
                        help="<collector> process records from only the given collector")
    parser.add_argument('--interval','-i', type=int, default=config.interval,
                        help="RIB table/ observers dump frequency")
    parser.add_argument('--peer_asn', '-j', default=config.peer_asns, nargs='+',
                        help="<peer_asn> process records from only the given peer_asn")
    parser.add_argument('--peer_ip', '-k', default=config.peer_ips, nargs='+',
                        help="<peer_ip> process records from only the given peer_ip")
    parser.add_argument('--time_format', '-l' ,default=config.time_fmt,
                        help="date format for parsing date_range and formatting output")

    # Parse the command-line arguments
    args = parser.parse_args()
        
    FORMAT = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(
        format=FORMAT,
        handlers=[
            logging.FileHandler(os.path.join(OUTPUT_DIR, f"{args.output_filename}.log")),
            logging.StreamHandler(sys.stdout)
        ],
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logging.info(f'Started: {sys.argv} with arguments {args}')
    
    session = CachedSession(os.path.join(OUTPUT_DIR, ".cache.sqlite"),
                            backend='sqlite', expire_after=-1)

    bgp_downloader = BGPDownloader(output_dir=OUTPUT_DIR,
                                       output_filename=args.output_filename,
                                       date_range=args.date_range,
                                       collectors=args.collector,
                                       peer_asns=args.peer_asn,
                                       peer_ips=args.peer_ip,
                                       interval=args.interval,
                                       session=session,
                                       time_fmt=args.time_format)
    
    # No profiling 
    bgp_downloader.run()
    
    # Profiling
    # output_prof_file = os.path.join(args.output_dir, f"{args.output_filename}.prof")
    # cProfile.run('bgp_downloader.run()', filename=output_prof_file, sort='cumulative')
    # sp.run(['gprof2dot', '-f', 'pstats', output_prof_file, '-o', output_prof_file.replace('.prof', '.dot')])
    # sp.run(['dot', '-Tpng', output_prof_file.replace('.prof', '.dot'),'-o', output_prof_file.replace('.prof', '.png')])
