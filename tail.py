'''
    - A script to tail a file and push lines to Kafka.
    - It will be ideal to run this script on top a supervisor like
    *process control system*, during an event of unexpected network/cpu
    outages the script exits abnormally and supervisor should try to
    restart tailer repeatedly.
'''

import Queue
import logging
import os
import socket
import sys
import time
import shelve

from pykafka import KafkaClient

logging.basicConfig(
    format=
    '%(asctime)s.%(msecs)s:%(name)s:%(thread)d:%(levelname)s:%(process)d:%(message)s',
    level=logging.INFO)


class Tailer(object):
    """
    Implements tailing and heading functionality like GNU tail and head
    commands.
    """

    line_terminators = ('\r\n', '\n', '\r')

    def __init__(self, file_path, logger_name, read_size=1024, end=False):
        self.read_size = read_size
        self.filepath = file_path
        self.inode_number = os.stat(file_path).st_ino
        try:
            self.file = open(file_path, 'rb')
            # Shelve location a combination of loggerName and log filename.
            self.shelve = shelve.open("/tmp/kakfa_tailer_offests_{}_{}".format(
                logger_name, self.filepath.split('/')[-1]))
            # If log has been rotated reset offset.
            try:
                if self.shelve['inode'] != self.inode_number:
                    self.shelve['offset'] = 0
                    self.shelve['inode'] = self.inode_number
            except:
                self.shelve['inode'] = self.inode_number
                self.shelve['offset'] = 0
            self.shelve.sync()
        except Exception, e:
            logging.error("Error shelving variables into file.")
            logging.error(str(e))
            sys.exit(1)
        if end:
            self.seek_end()

    def seek_end(self):
        self.seek(0, 2)

    def seek(self, pos, whence=0):
        self.file.seek(pos, whence)

    def follow(self, explicit_where=0, delay=0.001):
        """
        Iterator generator that returns lines as data is added to the file.
        Based on: http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/157035
        """
        trailing = True
        try:
            where = self.shelve['offset']
        except:
            where = 0

        if explicit_where > 0:
            where = explicit_where

        print "START", where
        # sanity check
        ost = os.stat(self.filepath)
        if ost.st_size < where:
            where = ost.st_size
        # A flag to limit number of fstat calls
        fstat_flag = 0
        while 1:
            line = self.file.readline()
            if line:
                if trailing and line in self.line_terminators:
                    # A line terminator added to the end of the file
                    # before a new line, ignore.
                    trailing = False
                    continue

                if line[-1] in self.line_terminators:
                    line = line[:-1]

                # print  where, "C :@", line, "@"
                trailing = False

                where = self.file.tell()
                yield line, where
                if explicit_where > 0:
                    #print "EXPLICIT WHERE, ", explicit_where
                    where = explicit_where
                    self.seek(where)

                #print where, self.file.tell()

                try:
                    self.shelve['prev_offset'] = where
                    self.shelve['offset'] = where
                    self.shelve.sync()
                except Exception, e:
                    logging.error("Shelve Exception")
                    logging.error(str(e))
                    continue
            else:
                trailing = True
                time.sleep(delay)
                #self.seek(where)
                # Check if log has been rotated/truncated
                try:
                    if fstat_flag == 10:
                        fstat_flag = 0
                        ost = os.stat(self.filepath)
                        mtime = ost.st_mtime
                        if (self.inode_number != ost.st_ino) or \
                            (ost.st_size < where): # or
                            #(ost.st_size == where and last_mtime != mtime) : # last case : file truncated same size
                            print "LOG CHANGED", where, ost.st_size
                            self.file.close()
                            self.file = open(self.filepath, 'rb')
                            self.inode_number = os.stat(self.filepath).st_ino
                            self.file.seek(0, 0)
                            self.shelve['inode'] = self.inode_number
                            self.shelve['offset'] = 0
                            self.shelve.sync()
                            where = 0
                    else:
                        fstat_flag += 1
                # If not, wait for new log to be created.
                except Exception, e:
                    logging.error("Wait fo new log to be created.")
                    logging.error(str(e))
                    time.sleep(delay * 20.0)

    def __iter__(self):
        return self.follow()


class KafkaProd(object):
    """Updates Kafka Cluster with logs in async batches"""

    def __init__(self,
                 kafka_url,
                 filepath,
                 topic_name,
                 logger_name,
                 ip_address,
                 batch_size,
                 batch_timeout,
                 truncate=0):
        self.inode_number = os.stat(filepath).st_ino
        self.file_path = filepath
        self.batch_size = batch_size
        self.logger_name = logger_name
        self.ip_address = ip_address
        self.kafka_url = kafka_url
        self.batch_timeout = float(batch_timeout)
        self.topic_name = topic_name
        self.truncate = truncate

    def get_kafka_client(self):
        try:
            self.client = KafkaClient(hosts=self.kafka_url)
        except Exception, e:
            logging.error(
                "Check connection parameters, error establishing Kafka Connection.")
            logging.error(e)
            time.sleep(10)
            sys.exit(1)

    def push_logs(self):
        try:
            self.topic = self.client.topics[self.topic_name]
        except Exception, e:
            logging.error(
                "Seems like topic is unavailable in given Kafka Broker!.")
            logging.error(e)
            time.sleep(10)
            sys.exit(1)
        with self.topic.get_producer(
                delivery_reports=True,
                linger_ms=self.batch_timeout,
                ack_timeout_ms=20 * 1000,
                min_queued_messages=self.batch_size) as producer:
            count = 0
            where = 0
            # Continously tail for the log using log_tailer.py
            for line, upto in Tailer(self.file_path,
                                     self.logger_name,
                                     end=True).follow(where, 0.0001):
                if len(line) < 2:
                    continue
                count += 1
                producer.produce("{}\t{}\t{}".format(self.logger_name, line,
                                                     self.ip_address),
                                 partition_key="{}".format(self.ip_address))
                # Check for every nth batch for acknowledgement
                if count == (self.batch_size * 5):
                    if self.truncate > 0:
                        # Truncate is not very effiecnt
                        print "Truncating.. to "
                        f = open(self.file_path, "w")
                        f.truncate(upto)
                        # goto end
                        f.seek(0, 2)
                        print "NEW", upto, where
                        where = f.tell()

                        f.close()
                    count = 0
                    success = 0
                    fail = 0
                    while True:
                        try:
                            msg, exc = producer.get_delivery_report(
                                block=False)
                            if exc is not None:
                                logging.warn(
                                    "Failed to deliver msg to partition {}: {}".format(
                                        msg.partition_key, repr(exc)))
                                fail += 1
                                if fail >= self.batch_size:
                                    # End abnormally and let supervisor restart me.
                                    sys.exit(1)
                            else:
                                fail = 0
                                success += 1
                                logging.debug("Success")
                        except Queue.Empty:
                            time.sleep(.2)
                            logging.info("Done {}".format(success))
                            break


if __name__ == '__main__':
    if len(sys.argv) < 7:
        print "Usage : python tail.py <kafka_url> <logpath> \
        <topic_name> <logger_name> > <batch_size>  <batch_timeout_ms> OPTIONAL : <truncate_1>"

        sys.exit(1)

    kafka_url = sys.argv[1]
    filepath = sys.argv[2]
    topic_name = sys.argv[3]
    logger_name = sys.argv[4]
    batch_size = int(sys.argv[5])
    batch_timeout = int(sys.argv[6])
    try:
        truncate = int(sys.argv[7]) > 0
    except:
        truncate = 0
    ip_address = str(socket.gethostname())
    kp = KafkaProd(kafka_url, filepath, topic_name, logger_name, ip_address,
                   batch_size, batch_timeout, truncate)
    # Connect to Kafka Client
    kp.get_kafka_client()
    # Push logs to Kakfa
    kp.push_logs()
