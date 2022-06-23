"""Controller and helpers for alert digests."""
import datetime
import logging
from collections import defaultdict
from typing import DefaultDict, Dict, List, Sequence, Set, Tuple

from chaos_genius.alerts.anomaly_alerts import (
    AnomalyPointFormatted,
    get_top_anomalies_and_counts,
)
from chaos_genius.alerts.constants import ALERT_DATE_FORMAT, FREQUENCY_DICT
from chaos_genius.alerts.slack import alert_digest_slack_formatted
from chaos_genius.alerts.utils import send_email_using_template
from chaos_genius.controllers.config_controller import get_config_object
from chaos_genius.controllers.digest_controller import (
    AlertsReportData,
    TriggeredAlertWithPoints,
    get_alert_kpi_configurations,
    preprocess_triggered_alert,
)
from chaos_genius.databases.models.alert_model import Alert
from chaos_genius.databases.models.kpi_model import Kpi
from chaos_genius.databases.models.triggered_alerts_model import (
    TriggeredAlerts,
    triggered_alerts_data_datetime,
)
from chaos_genius.settings import DAYS_OFFSET_FOR_ANALTYICS

logger = logging.getLogger(__name__)

ALERT_ATTRIBUTES_MAPPER = {"daily": "daily_digest", "weekly": "weekly_digest"}


class AlertDigestController:
    """Controller for anomaly alert digests."""

    def __init__(self, frequency: str):
        """Initializes an anomaly alert digests controller.

        Note: an AlertDigestController instance must only be used for one check/trigger
        of a digest. The same object must not be re-used.

        Arguments:
            frequency: digest frequency. See keys of FREQUENCY_DICT for possible values.
        """
        self.time_diff = FREQUENCY_DICT[frequency]
        self.curr_time = datetime.datetime.now()
        self.alert_config_cache: Dict[int, Alert] = dict()
        self.kpi_cache: Dict[int, Kpi] = dict()
        self.frequency = frequency

    def check_and_send_digests(self):
        """Collects alerts to be sent and sends them to respective channels.

        Note: must only be called once on an instance.
        """
        report_date, start_date, end_date = self._data_time_range()
        triggered_alerts = self._get_triggered_alerts(start_date, end_date)

        slack_alerts: List[TriggeredAlertWithPoints] = []
        email_alerts: List[TriggeredAlertWithPoints] = []

        self.alert_config_cache, self.kpi_cache = get_alert_kpi_configurations(
            triggered_alerts
        )

        for triggered_alert in triggered_alerts:
            triggered_alert = preprocess_triggered_alert(
                triggered_alert, self.alert_config_cache, self.kpi_cache
            )

            if not triggered_alert.points:
                logger.info(
                    f"(frequency: {self.frequency}) "
                    "No overall anomalies found for TriggeredAlerts with id %d, "
                    "skipping.",
                    triggered_alert.id,
                )
            else:
                if getattr(
                    self.alert_config_cache[triggered_alert.alert_conf_id],
                    ALERT_ATTRIBUTES_MAPPER[self.frequency],
                ):
                    if triggered_alert.alert_channel == "slack":
                        slack_alerts.append(triggered_alert)
                    if triggered_alert.alert_channel == "email":
                        email_alerts.append(triggered_alert)

        if len(email_alerts) > 0:
            report_data = AlertsReportData.from_triggered_alerts(
                triggered_alerts=email_alerts, report_date=report_date
            )
            self._send_email_digests(report_data)

        if len(slack_alerts) > 0:
            report_data = AlertsReportData.from_triggered_alerts(
                triggered_alerts=slack_alerts, report_date=report_date
            )
            self._send_slack_digests(report_data)

    def _data_time_range(
        self,
    ) -> Tuple[datetime.date, datetime.datetime, datetime.datetime]:
        anomaly_date = datetime.date.today() - datetime.timedelta(
            days=DAYS_OFFSET_FOR_ANALTYICS
        )

        # datetime.time() is initialized to 00:00
        start_date = datetime.datetime.combine(anomaly_date, datetime.time())

        end_date = start_date + datetime.timedelta(days=1)

        logger.info(
            "Got time range for digest check: [%s, %s)",
            start_date.isoformat(),
            end_date.isoformat(),
        )

        return anomaly_date, start_date, end_date

    def _get_triggered_alerts(
        self, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> List[TriggeredAlerts]:
        return (
            TriggeredAlerts.query.filter(
                (triggered_alerts_data_datetime() >= start_date)
                & (triggered_alerts_data_datetime() < end_date)
            )
            .order_by(TriggeredAlerts.created_at.desc())
            .all()
        )

    def _send_email_digests(self, data: AlertsReportData):
        user_triggered_alerts: DefaultDict[str, Set[int]] = defaultdict(set)
        for triggered_alert in data.triggered_alerts:
            for user in triggered_alert.alert_channel_conf:
                user_triggered_alerts[user].add(triggered_alert.id)

        triggered_alert_dict: Dict[int, TriggeredAlertWithPoints] = {
            alert.id: alert for alert in data.triggered_alerts
        }
        for recipient in user_triggered_alerts.keys():
            self._send_email_digest(
                recipient,
                AlertsReportData.from_triggered_alerts(
                    [
                        triggered_alert_dict[id_]
                        for id_ in user_triggered_alerts[recipient]
                    ],
                    data.report_date,
                ),
            )

    def _get_top_anomalies_and_counts(
        self, triggered_alerts: List[TriggeredAlertWithPoints]
    ) -> Tuple[Sequence[AnomalyPointFormatted], int, int]:
        return get_top_anomalies_and_counts(
            [
                point
                for triggered_alert in triggered_alerts
                for point in triggered_alert.points
            ],
            n=10,
        )

    def _send_email_digest(self, recipient: str, data: AlertsReportData):
        send_email_using_template(
            "digest_template.html",
            [recipient],
            (
                f"Daily Alerts Report ({data.report_date_formatted()}) - "
                "Chaos Genius Alert❗"
            ),
            [],
            preview_text="",
            data=data,
        )

    def _send_slack_digests(
        self,
        data: AlertsReportData,
    ):
        """Sends a slack alert containing a summary of triggered alerts."""
        err = alert_digest_slack_formatted(data)

        if err == "":
            logger.info(
                f"(frequency: {self.frequency}) The slack alert digest was successfully"
                " sent"
            )
        else:
            raise Exception(
                f"(frequency: {self.frequency}) Error in sending slack digest: {err}"
            )


def check_and_trigger_digest(frequency: str):
    """Check the alert and trigger alert digest.

    Args:
        frequency (str): frequency of alert digest

    Raises:
        Exception: Raise if digest frequency is incorrect or alert digests have not
            been configured
    """
    if frequency not in ALERT_ATTRIBUTES_MAPPER.keys():
        msg = f"Alert Digest frequency is not valid. Got: {frequency}."
        logger.error(msg)
        raise Exception(msg)

    digest_config = get_config_object("alert_digest_settings")

    if digest_config is None:
        msg = "Alert digests have not been enabled."
        logger.error(msg)
        raise Exception(msg)

    digest_config_settings: dict = digest_config.config_setting

    if not digest_config_settings.get(ALERT_ATTRIBUTES_MAPPER[frequency]):
        msg = f"Digests with frequency {frequency} have not been enabled."
        logger.info(msg)
        raise Exception(msg)

    digest_obj = AlertDigestController(frequency)
    digest_obj.check_and_send_digests()
