from queue import Queue, Empty
from datetime import datetime
from threading import Thread
import pandas as pd

import config, tools


class Sensor:
    def __init__(self, stream, batch_size = config.BATCH_SIZE, reuse_size = config.REUSE_SIZE):
        self.stream = stream
        self.batch_size = batch_size
        self.reuse_size = reuse_size
        self.data_queue = Queue()

        self.accumulated_raw = None
        self.accumulated_processed = None
        self.should_record = False
        self.reset() # initializes these variables

        # The method that actually reads from the device needs to be in its own thread.
        # This way, if there's nothing coming from it, we aren't holding up the GUI thread.
        self._data_fetching_thread = Thread(target=_data_fetching_loop, args=(self.stream, self.data_queue, self.batch_size))
        self._data_fetching_thread.start()

    # Basically the core of the research problem-- turn raw acceleration into meaningful position
    def process_next_batch(self):
        try:
            # Collect new raw acceleration data, if available.
            # Otherwise, throws Empty exception
            log_message(2, "Fetching data")
            new_bytes = self.data_queue.get_nowait()

            # Don't process/save this data if we don't want it
            if(not self.should_record):
                return

            # Parse the bytes into a pandas data frame
            new_samples = []

            for line in new_bytes:
                try:
                    line = line.decode("utf-8").strip()

                    if line.startswith("mugicdata"): # Keep only lines with mugicdata prefix
                        new_samples.append(line.split(' ')[1:])  # but lose that prefix
                
                except:
                    log_message(1, "A line of data was corrupt. This is likely because you are running on serial mode and read the data mid-line. This line will be thrown out and is probably nothing to worry about.")

            new_samples = pd.DataFrame(new_samples, columns = config.COLUMNS, dtype = "double").set_index("time_sec")

            # Don't calculate position if we haven't calibrated enough
            # ("Calibrating" == accumulating enough data to fulfill reuse size requirement)
            if self.needs_calibration():
                log_message(1, f"Calibrating ({self.accumulated_raw.shape[0]} samples), hold still...")

                self.accumulated_raw = self.accumulated_raw.append(new_samples)

                # If we have enough data now, integrate it all at once so we have previous positions to work with
                if not self.needs_calibration(): 
                    self.accumulated_processed = self.accumulated_processed.append(self.accumulated_raw[["ax", "ay", "az", "qw", "qx", "qy", "qz"]])

                    # Convert to linear acceleration
                    rotation_matrices = tools.quaternions_as_rotation_matrix(self.accumulated_processed.qw, self.accumulated_processed.qx, self.accumulated_processed.qy, self.accumulated_processed.qz)
                    new_linear_acceleration = tools.rotate_each_row(self.accumulated_processed[["ax", "ay", "az"]], rotation_matrices) \
                        .rename(columns = {"ax": "lax", "ay": "lay", "az": "laz"}) \
                        .set_index(self.accumulated_processed.index) # the tool will throw out the index

                    self.accumulated_processed[["lax", "lay", "laz"]] = new_linear_acceleration

                    # Get measured velocity by filtering this acceleration and integrating
                    new_velocity = self.accumulated_processed[["lax", "lay", "laz"]] \
                        .apply(lambda col: tools.filter_and_integrate(col, self.accumulated_processed.index)) \
                        .rename(columns = {"lax": "vx", "lay": "vy", "laz": "vz"})

                    self.accumulated_processed[["vx", "vy", "vz"]] = new_velocity

                    # Get measured position by filtering this acceleration and integrating
                    new_position = self.accumulated_processed[["vx", "vy", "vz"]] \
                        .apply(lambda col: tools.filter_and_integrate(col, self.accumulated_processed.index)) \
                        .rename(columns = {"vx": "x", "vy": "y", "vz": "z"})

                    self.accumulated_processed[["x", "y", "z"]] = new_position

                    # Perform principal component analysis on the velocity and position
                    _, velocity_PCs = tools.PCA(self.accumulated_processed[["vx", "vy", "vz"]])
                    position_matrix, position_PCs = tools.PCA(self.accumulated_processed[["x", "y", "z"]])

                    self.accumulated_processed["velocity"] = velocity_PCs.PC1
                    self.accumulated_processed["position"] = position_PCs.PC1

                    # Project position onto the plane perpendicular to the direction of most motion
                    eig1 = position_matrix[:, 0]
                    projected_points = tools.project_3D_to_2D(self.accumulated_processed[["x", "y", "z"]].to_numpy(), eig1)

                    self.accumulated_processed["projected_X"] = projected_points[:, 0]
                    self.accumulated_processed["projected_Y"] = projected_points[:, 1]

                    log_message(1, "Done calibrating.")

            # Otherwise, begin the batch-by-batch positioning algorithm
            else:
                # Ensure our accumulated data is in chronological order
                self.sort_accumulated_data()

                log_message(2, "Processing batch")

                # Convert to linear acceleration
                rotation_matrices = tools.quaternions_as_rotation_matrix(new_samples.qw, new_samples.qx, new_samples.qy, new_samples.qz)
                new_linear_acceleration = tools.rotate_each_row(new_samples[["ax", "ay", "az"]], rotation_matrices) \
                    .rename(columns = {"ax": "lax", "ay": "lay", "az": "laz"}) \
                    .set_index(new_samples.index) # the tool will throw out the index

                new_samples[["lax", "lay", "laz"]] = new_linear_acceleration

                # Get measured velocity by filtering this acceleration with previous samples, and integrating
                new_velocity = self.accumulated_processed[["lax", "lay", "laz"]] \
                    .tail(self.reuse_size) \
                    .append(new_samples[["lax", "lay", "laz"]]) \
                    .apply(lambda col: tools.filter_and_integrate(col, new_samples.index)) \
                    .rename(columns = {"lax": "vx", "lay": "vy", "laz": "vz"})

                ## Integration correction ##

                # Retrieve average velocity of previous batch
                # (or specifically, the portion of that batch that overlaps with this one)
                #mean_old_v = self.accumulated_processed[["vx", "vy", "vz"]] \
                #    .tail(self.reuse_size - self.batch_size) \
                #    .mean(axis = 0)

                # Retrieve average velocity of new batch
                # (again, specifically the portion that overlaps)
                #mean_new_v = new_velocity \
                #    .head(self.reuse_size - self.batch_size) \
                #    .mean(axis = 0)

                # Add a constant to velocity such that mean_old_v == mean_new_v
                #offset = mean_old_v - mean_new_v
                #new_velocity = new_velocity.apply(lambda row: row + offset,
                #                                  axis = 1)

                ## End integration correction ##

                new_samples[["vx", "vy", "vz"]] = new_velocity.tail(self.batch_size)

                # Get measured position by filtering this velocity with previous samples, and integrating
                new_position = self.accumulated_processed[["vx", "vy", "vz"]] \
                    .tail(self.reuse_size) \
                    .append(new_samples[["vx", "vy", "vz"]]) \
                    .apply(lambda col: tools.filter_and_integrate(col, new_samples.index)) \
                    .rename(columns = {"vx": "x", "vy": "y", "vz": "z"})

                ## Integration correction ##

                # Retrieve average position of previous batch
                # (or specifically, the portion of that batch that overlaps with this one)
                #mean_old_p = self.accumulated_processed[["x", "y", "z"]] \
                #    .tail(self.reuse_size - self.batch_size) \
                #    .mean(axis = 0)

                # Retrieve average position of new batch
                # (again, specifically the portion that overlaps)
                #mean_new_p = new_position \
                #    .head(self.reuse_size - self.batch_size) \
                #    .mean(axis = 0)

                # Add a constant to position such that mean_old_p == mean_new_p
                #offset = mean_old_p - mean_new_p
                #new_position = new_position.apply(lambda row: row + offset,
                #                                  axis = 1)

                ## End integration correction ##

                new_samples[["x", "y", "z"]] = new_position.tail(self.batch_size)

                # Perform principal component analysis on the velocity and position
                _, velocity_PCs = tools.PCA(new_samples[["vx", "vy", "vz"]])
                position_matrix, position_PCs = tools.PCA(new_samples[["x", "y", "z"]])

                new_samples["velocity"] = velocity_PCs.PC1
                new_samples["position"] = position_PCs.PC1

                # Project position onto the plane perpendicular to the direction of most motion
                eig1 = position_matrix[:, 0]
                projected_points = tools.project_3D_to_2D(new_samples[["x", "y", "z"]].to_numpy(), eig1)

                new_samples["projected_X"] = projected_points[:, 0]
                new_samples["projected_Y"] = projected_points[:, 1]

                self.accumulated_processed = self.accumulated_processed.append(new_samples)
            
        except Empty:
            # The data fetching thread had nothing, so don't hold up the GUI thread... just return out
            log_message(2, "No data was queued.")
    
        except Exception as ex:
            log_message(1, repr(ex))

    def close_stream(self):
        self.stream.close()

    def get_latest_n_samples(self, n):
        # Ensure our accumulated data is in chronological order
        self.sort_accumulated_data()

        return self.accumulated_processed.tail(n)
    
    def needs_calibration(self):
        return self.accumulated_raw.shape[0] < self.reuse_size + self.batch_size

    def reset(self):
        self.accumulated_raw = pd.DataFrame(columns=config.COLUMNS) \
            .set_index("time_sec")

        self.accumulated_processed = pd.DataFrame(columns=config.ACCUMULATED_COLUMNS) \
            .set_index("time_sec")

        self.should_record = False

    def sort_accumulated_data(self):
        if(not self.accumulated_processed.index.is_monotonic_increasing):
            self.accumulated_processed.sort_index(inplace = True)

    # Enables/disables the processing of incoming data
    def toggle_recording(self):
        self.should_record = not self.should_record

        return self.should_record


# Printing, but with timestamps!
def log_message(error_level, msg):
    if(error_level <= config.DEBUG_LEVEL):
        print(datetime.now(), '\t', msg)

# Constantly pushes data from the sensor into a queue for processing on demand
def _data_fetching_loop(stream, data_queue, batch_size):
    import time
    start = time.time()
    last_time = start
    iterations = 0

    try:
        while True:
            raw_bytes = stream.readlines(batch_size)
            data_queue.put(raw_bytes)

            iterations += 1
            now = time.time()
            log_message(1, f"Time since last iteration: {round(now - last_time, 2)}")
            log_message(1, f"Average iters/sec: {round(iterations / (now - start), 2)}")
            log_message(1, "----------------------------")
            last_time = now

    except:  # the connection was closed, so this thread needs to end
            log_message(1, "Fetching has been halted.")
