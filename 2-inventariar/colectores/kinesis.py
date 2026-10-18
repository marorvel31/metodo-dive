"""Kinesis Data Streams y Firehose delivery streams."""

from common import recurso
from common.aws import client

NAME = "kinesis"
TRIGGERS = ["Amazon Kinesis", "Amazon Kinesis Firehose"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def _firehose(session, region, account_id):
    fh = client(session, "firehose", region)
    recursos, desde = [], None
    # list_delivery_streams no tiene paginador: se pagina a mano.
    while True:
        kw = {"Limit": 100, **({"ExclusiveStartDeliveryStreamName": desde} if desde else {})}
        resp = fh.list_delivery_streams(**kw)
        for nombre in resp.get("DeliveryStreamNames", []):
            d = fh.describe_delivery_stream(DeliveryStreamName=nombre)["DeliveryStreamDescription"]
            # Solo el tipo de cada destino: la config de destino puede traer URLs y credenciales.
            destinos = sorted({k.replace("DestinationDescription", "")
                               for dest in d.get("Destinations", []) for k in dest if k.endswith("DestinationDescription")})
            recursos.append(recurso(
                account_id, region, NAME, "firehose", nombre,
                arn=d.get("DeliveryStreamARN"),
                name=nombre,
                state=d.get("DeliveryStreamStatus"),
                attrs={
                    "source_type": d.get("DeliveryStreamType"),
                    "destinations": destinos,
                    "encryption": (d.get("DeliveryStreamEncryptionConfiguration") or {}).get("Status"),
                },
            ))
        if not resp.get("HasMoreDeliveryStreams") or not resp.get("DeliveryStreamNames"):
            return recursos
        desde = resp["DeliveryStreamNames"][-1]


def collect(session, region, account_id):
    kin = client(session, "kinesis", region)
    recursos = []
    for pagina in kin.get_paginator("list_streams").paginate():
        for nombre in pagina["StreamNames"]:
            s = kin.describe_stream_summary(StreamName=nombre)["StreamDescriptionSummary"]
            recursos.append(recurso(
                account_id, region, NAME, "stream", nombre,
                arn=s.get("StreamARN"),
                name=nombre,
                state=s.get("StreamStatus"),
                attrs={
                    "mode": (s.get("StreamModeDetails") or {}).get("StreamMode", "PROVISIONED"),
                    "open_shards": s.get("OpenShardCount"),
                    "retention_hours": s.get("RetentionPeriodHours"),
                    "encryption": s.get("EncryptionType"),
                    "consumers": s.get("ConsumerCount"),
                },
            ))
    return recursos + _firehose(session, region, account_id)
