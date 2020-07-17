import pyqtgraph as pg
import pandas as pd

from datetime import datetime
from queue import Queue, Empty
from threading import Thread

import config, tools


class PlotDisplay(pg.GraphicsLayoutWidget):

    def __init__(self, stream):
        pg.GraphicsLayoutWidget.__init__(self) # super() doesn't seem to work here

        # Storing accumulated data
        self.accumulated_raw = pd.DataFrame(columns=["time_sec", "ax", "ay", "az", "qw", "qx", "qy", "qz"])
        self.accumulated_processed = pd.DataFrame(columns=["time_sec", "PC1", "projected_X", "projected_Y"])

        # Application data
        self.data_queue = Queue()
        self.plots      = [ self.addPlot() for _ in range(2) ]
        self.curves     = [ plot.plot() for plot in self.plots ]
        self.stream     = stream

        # Start the data collection process
        fetching_thread = Thread(target=retrieve_new_data, args=(self.stream, self.data_queue))
        fetching_thread.start()

    
    # Add the given data to the accumulated storage,
    # keeping at most the latest HISTORY number of samples
    def _accumulate_raw_data(self, raw_data):
        self.accumulated_raw = self.accumulated_raw.append(
            raw_data[self.accumulated_raw.columns],
            ignore_index = True
        )

    
    def _accumulate_processed_data(self, processed_data):
        self.accumulated_processed = self.accumulated_processed.append(
            processed_data[self.accumulated_processed.columns],
            ignore_index = True
        )

    # Note that the data withheld for calibration won't have been processed, but will be exported    
    def export_accumulated_data(self, filename):
        log_message(2, "Exporting accumulated data")

        pd.merge(self.accumulated_raw, self.accumulated_processed, how = "outer", on = "time_sec") \
            .to_csv(filename)


    def _update_graphics(self):
        self.curves[0].setData(
            self.accumulated_processed.time_sec.tail(config.HISTORY).reset_index(drop = True), # pg depends on the first index being 0
            self.accumulated_processed.PC1.tail(config.HISTORY).reset_index(drop = True)
        )

        self.curves[1].setData(
            self.accumulated_processed.projected_X.tail(config.HISTORY).reset_index(drop = True),
            self.accumulated_processed.projected_Y.tail(config.HISTORY).reset_index(drop = True)
        )

        self.plots[0].setYRange(
            min(-0.2, self.accumulated_processed.PC1.min()),
            max( 0.2, self.accumulated_processed.PC1.max())
        )

        self.plots[1].setXRange(
            min(-0.1, self.accumulated_processed.projected_X.min()),
            max( 0.1, self.accumulated_processed.projected_X.max())
        )

        self.plots[1].setYRange(
            min(-0.1, self.accumulated_processed.projected_Y.min()),
            max( 0.1, self.accumulated_processed.projected_Y.max())
        )

    def _fetch_data(self):
        log_message(2, "Fetching data")

        samples = parse_bytes(self.data_queue.get_nowait())
        self._accumulate_raw_data(samples)

        # Make sure we have enough data before we run any filters
        assert self.accumulated_raw.shape[0] >= config.REUSE_SIZE + config.SAMPLE_SIZE

        # When integrating/filtering/etc, throw in some old data too...
        log_message(2, "Processing data")

        processed_data = process_data(self.accumulated_raw.tail(config.REUSE_SIZE + config.SAMPLE_SIZE))

        # ...but still only accumulate the new data
        self._accumulate_processed_data(processed_data.tail(config.SAMPLE_SIZE))

    # Plot loop
    def update(self):
        try: 
            # Get and process new data, if available
            self._fetch_data()

            # Update the plots with the new data
            self._update_graphics()
        
        except Empty:
            # No more data is ready yet, let's not hold up the main UI
            pass

        except AssertionError:
            # We haven't collected enough data to begin analysis yet
            log_message(1, "Withholding data for calibration")

        except Exception as ex:
            log_message(1, repr(ex))


# Converts the raw output from the sensor to a Pandas data frame
def parse_bytes(raw_data: [bytes]) -> pd.DataFrame:
    rows = []

    for line in raw_data:
        try:
            line = line.decode("utf-8").strip()

            if line.startswith("mugicdata"): # Keep only lines with mugicdata prefix
                rows.append(line.split(' ')[1:])  # but lose that prefix
        
        except:
            print("A line of data was corrupt. This is likely because you are running on serial mode and read the data mid-line. This line will be thrown out and is probably nothing to worry about.")

    return pd.DataFrame(rows, dtype="double", columns = config.COLUMNS)


def log_message(error_level, msg):
    if(error_level <= config.DEBUG_LEVEL):
        print(datetime.now(), '\t', msg)


# The positioning algorithm - CURRENTLY DISPLAYING LAX INSTEAD OF PC1
def process_data(samples):
    raw_acceleration = samples[["ax", "ay", "az"]]

    rotation_matrices = tools.quaternions_as_rotation_matrix(samples.qw, samples.qx, samples.qy, samples.qz)
    linear_acceleration = tools.rotate_each_row(raw_acceleration, rotation_matrices)

    # Get dataframe of x,y,z
    position = linear_acceleration \
        .apply(lambda col: tools.filter_and_integrate(col, samples.time_sec)) \
        .apply(lambda col: tools.filter_and_integrate(col, samples.time_sec))

    pca_matrix, pca_data = tools.PCA(position)

    eig1 = pca_matrix[:, 0]
    new_points = tools.project_3D_to_2D(position.to_numpy(), eig1)

    # TODO: export everything so we can see if position/velocity look right
    return pd.DataFrame({
        "time_sec":    samples.time_sec.values, # .values removes the pd index that throws off DataFrame()
        "PC1":         pca_data.PC1,
        "projected_X": new_points[:, 0],
        "projected_Y": new_points[:, 1]
    })


def retrieve_new_data(stream, data_queue):
    try:
        while True:
            raw_data = stream.readlines(n=config.SAMPLE_SIZE)
            data_queue.put(raw_data)

    except:  # the connection was closed, so this thread needs to end
        log_message(2, "Fetching has been halted.")