'''
    Tails logs with *log_tailer.py* and pushes them to Kafka Server
    in async batches.
'''
import Queue
import logging
import os
import socket
import sys
import time
import shelve

from pykafka import KafkaClient

# create logger
logging.basicConfig(
        format='%(asctime)s.%(msecs)s:%(name)s:%(thread)d:%(levelname)s:\
    %(process)d:%(message)s',
        level=logging.INFO)


class Tailer(object):
    """
    Implements tailing and heading functionality like GNU tail and head
    commands.
    """

    line_terminators = ('\r\n', '\n', '\r')

    def __init__(self, file_path, read_size=1024, end=False):
        self.read_size = read_size
        self.filepath = file_path
        self.inode_number = os.stat(file_path).st_ino
        self.file = open(file_path, 'rb')
        self.start_pos = self.file.tell()
        self.shelve = shelve.open("/tmp/kakfa_tailer_offests")
        if end:
            self.seek_end()

    def seek_end(self):
        self.seek(0, 2)

    def seek(self, pos, whence=0):
        self.file.seek(pos, whence)

    def follow(self, delay=0.01):
        """
        Iterator generator that returns lines as data is added to the file.
        Based on: http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/157035
        """
        trailing = True
        while 1:
            where = self.shelve.get('offset', 0)
            line = self.file.readline()
            print "WHERE =",where
            if line:


                if trailing and line in self.line_terminators:
                    # This is just the line terminator added to the end of the file
                    # before a new line, ignore.
                    trailing = False
                    continue

                if line[-1] in self.line_terminators:
                    line = line[:-1]


                trailing = False
                self.shelve['prev_offset'] = self.shelve['offset']
                yield line
                self.shelve['offset'] = self.file.tell()
            else:
                trailing = True
                # print "SEEK : ", where
                self.seek(where)
                time.sleep(delay)
                # Check if log has been rotated
                try:
                    ost = os.stat(self.filepath)
                    if (self.inode_number != ost.st_ino) or (ost.st_size < where ):
                        print "LOG CHANGED"
                        self.file.close()
                        self.file = open(self.filepath, 'rb')
                        self.inode_number = os.stat(self.filepath).st_ino
                        self.file.seek(0, 0)
                        self.shelve['offset'] = 0
                # If not, wait for new log to be created.
                except (OSError, IOError):
                    print "EXCEPT"
                    time.sleep(delay * 5.0)


    def __iter__(self):
        return self.follow()


class KafkaProd(object):
    """Updates Kafka Cluster with logs in async batches"""

    def __init__(self, kafka_url, filepath, topic_name, logger_name,
                 ip_address, batch_size, batch_timeout):
        self.inode_number = os.stat(filepath).st_ino
        self.file_path = filepath
        self.batch_size = batch_size
        self.logger_name = logger_name
        self.ip_address = ip_address
        self.kafka_url = kafka_url
        self.batch_timeout = batch_timeout
        self.topic_name = topic_name

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
            # Continously tail for the log using log_tailer.py
            for line in Tailer(filepath, end=True).follow(2):
                count += 1
                producer.produce("{}\t{}\t{}".format(self.logger_name, line,
                                                     self.ip_address),
                                 partition_key="{}".format(self.ip_address))
                logging.debug(count, line)
                # Check for every 100th batch for acknowledgement
                if count == (self.batch_size * 10):
                    count = 0
                    success = 0
                    fail = 0
                    while True:
                        try:
                            msg, exc = producer.get_delivery_report(
                                    block=False)
                            if exc is not None:
                                logging.warn(
                                        "Failed to deliver msg {}: {}".format(
                                                msg.partition_key, repr(exc)))
                                fail += 1
                                if fail >= self.batch_size:
                                    sys.exit(1)
                            else:
                                fail = 0
                                success += 1
                                logging.debug("Success")
                        except Queue.Empty:
                            logging.debug("Empty Queue")
                            logging.info("Done {}".format(success))
                            time.sleep(.2)
                            break


if __name__ == '__main__':
    if len(sys.argv) < 7:
        print "Usage : python tail.py <kafka_url> <logpath>\
        <topic_name> <logger_name> > <batch_size>  <batch_timeout_ms>"

        sys.exit(1)

    batch_size = int(sys.argv[5])
    batch_timeout = int(sys.argv[6])
    kafka_url = sys.argv[1]
    filepath = sys.argv[2]
    topic_name = sys.argv[3]
    logger_name = sys.argv[4]
    ip_address = str(socket.gethostname())
    kp = KafkaProd(kafka_url, filepath, topic_name, logger_name, ip_address,
                   batch_size, batch_timeout)
    # Connect to Kafka Client
    kp.get_kafka_client()
    # Push logs to Kakfa
    kp.push_logs()
