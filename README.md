# Kafka Tailer
## Tail a file and pipe to kafka

Usage : 
```
python tail.py <kafka_url> <logpath> <topic_name> <logger_name> > <batch_size>  <batch_timeout_ms> <optional : truncate>

```

Be careful with `batch_timeout`, the same variable is used to calculate delay in tail loop to reduce cpu cycles.
Recomended `batch_timeout` is `10`. 
