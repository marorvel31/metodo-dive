"""Tópicos SNS. Solo conteos de suscripciones: los endpoints (emails, URLs) no se copian."""

from common import recurso
from common.aws import client

NAME = "sns"
TRIGGERS = ["Amazon Simple Notification Service"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    sns = client(session, "sns", region)
    recursos = []
    for pagina in sns.get_paginator("list_topics").paginate():
        for t in pagina["Topics"]:
            arn = t["TopicArn"]
            # Policy y DeliveryPolicy se ignoran; list_subscriptions no se llama.
            # Un tópico con policy restrictiva puede negar la lectura: se registra igual, sin atributos.
            try:
                a = sns.get_topic_attributes(TopicArn=arn)["Attributes"]
            except Exception as e:  # noqa: BLE001
                a = {"_error": getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__}
            try:
                tags = sns.list_tags_for_resource(ResourceArn=arn).get("Tags", [])
            except Exception:  # noqa: BLE001
                tags = []
            nombre = arn.rsplit(":", 1)[-1]
            recursos.append(recurso(
                account_id, region, NAME, "topic", nombre,
                arn=arn,
                name=nombre,
                tags=tags,
                attrs={
                    "subscriptions_confirmed": int(a.get("SubscriptionsConfirmed", 0)),
                    "subscriptions_pending": int(a.get("SubscriptionsPending", 0)),
                    "fifo": a.get("FifoTopic") == "true",
                    "kms_key_id": a.get("KmsMasterKeyId"),
                    "attributes_error": a.get("_error"),
                },
            ))
    return recursos
