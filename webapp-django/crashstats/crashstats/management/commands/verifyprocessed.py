# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
This command verifies that all the incoming crash reports for a specified day
were processed. It does this by listing the raw crash files for the day, then
checking to see if each of those raw crash files have corresponding processed
crash files.
"""

import concurrent.futures
import datetime
from functools import partial

import markus
from more_itertools import chunked

from django.core.management.base import BaseCommand, CommandError
from django.db.utils import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from crashstats.crashstats.configman_utils import get_s3_context
from crashstats.crashstats.models import MissingProcessedCrash


RAW_CRASH_PREFIX_TEMPLATE = "v2/raw_crash/%s/%s/"
PROCESSED_CRASH_TEMPLATE = "v1/processed_crash/%s"

# Number of seconds until we decide a worker has stalled
WORKER_TIMEOUT = 10 * 60


metrics = markus.get_metrics("cron.verifyprocessed")


def check_crashids(entropy_chunk, date):
    """Checks crash ids for a given entropy and date."""
    s3_context = get_s3_context()
    bucket = s3_context.config.bucket_name
    s3_client = s3_context.client

    missing = []
    for entropy in entropy_chunk:
        raw_crash_key_prefix = RAW_CRASH_PREFIX_TEMPLATE % (entropy, date)

        paginator = s3_client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=bucket, Prefix=raw_crash_key_prefix)

        for page in page_iterator:
            for item in page.get("Contents", []):
                raw_crash_key = item["Key"]
                crash_id = raw_crash_key.split("/")[-1]

                try:
                    s3_client.head_object(
                        Bucket=bucket, Key=PROCESSED_CRASH_TEMPLATE % crash_id
                    )
                except s3_client.exceptions.ClientError as exc:
                    # If we got back a 404: Not Found, then the processed crash isn't
                    # there. If we got something else back, re-raise it.
                    if exc.response["Error"]["Code"] == "404":
                        missing.append(crash_id)
                    else:
                        raise

    return missing


class Command(BaseCommand):
    help = "Verify incoming crash reports were processed"

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-time",
            default="",
            help="The day to check in YYYY-mm-dd format. Defaults to yesterday.",
        )
        parser.add_argument(
            "--num-workers",
            default=20,
            type=int,
            help="Number of concurrent workers to list raw_crashes.",
        )

    def get_entropy(self):
        """Generate all entropy combinations."""
        chars = "0123456789abcdef"
        for x in chars:
            for y in chars:
                for z in chars:
                    yield x + y + z

    def find_missing(self, num_workers, date):
        check_crashids_for_date = partial(check_crashids, date=date)

        missing = []
        entropy_chunked = chunked(self.get_entropy(), 5)
        if num_workers == 1:
            for result in map(check_crashids_for_date, entropy_chunked):
                missing.extend(result)
        else:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=num_workers
            ) as executor:
                for result in executor.map(
                    check_crashids_for_date, entropy_chunked, timeout=WORKER_TIMEOUT
                ):
                    missing.extend(result)

        return list(missing)

    def handle_missing(self, date, missing):
        """Report crash ids for missing processed crashes."""
        metrics.gauge("missing_processed", len(missing))
        if missing:
            for crash_id in missing:
                self.stdout.write("Missing: %s" % crash_id)

                try:
                    MissingProcessedCrash.objects.create(
                        crash_id=crash_id, is_processed=False
                    )
                except IntegrityError as ie:
                    if "violates unique constraint" in str(ie):
                        # If there's already one, that's fine
                        pass
                    else:
                        raise
        else:
            self.stdout.write("All crashes for %s were processed." % date)

    def check_past_missing(self):
        """Check the table for missing crashes and check to see if they exist."""
        s3_context = get_s3_context()
        bucket_name = s3_context.config.bucket_name
        s3_client = s3_context.build_client()

        crash_ids = []

        crash_ids = MissingProcessedCrash.objects.filter(
            is_processed=False
        ).values_list("crash_id", flat=True)

        no_longer_missing = []

        for crash_id in crash_ids:
            bucket = s3_client.get_bucket(bucket_name)
            processed_crash_key = bucket.get_key(PROCESSED_CRASH_TEMPLATE % crash_id)
            if processed_crash_key is not None:
                no_longer_missing.append(crash_id)

        updated = 0
        if no_longer_missing:
            updated = MissingProcessedCrash.objects.filter(
                crash_id__in=no_longer_missing
            ).update(is_processed=True)

        self.stdout.write(
            "Updated %s missing crashes which have since been processed" % updated
        )

    def handle(self, **options):
        check_date_arg = options.get("run_time")
        if check_date_arg:
            check_date = parse_datetime(check_date_arg)
            if not check_date:
                check_date = parse_date(check_date_arg)
            if not check_date:
                raise CommandError("Unrecognized run_time format: %s" % check_date_arg)
        else:
            check_date = timezone.now() - datetime.timedelta(days=1)

        check_date_formatted = check_date.strftime("%Y%m%d")

        # Check and update existing missing before finding new missing things
        self.check_past_missing()

        # Find missing and handle them
        missing = self.find_missing(options["num_workers"], check_date_formatted)
        self.handle_missing(check_date_formatted, missing)

        self.stdout.write("Done!")
