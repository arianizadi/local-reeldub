#!/usr/bin/env python3
"""Run in the account owner's AWS CLI or CloudShell. Return only encrypted writer credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import boto3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certificate", type=Path, required=True, help="PEM public recovery certificate")
    parser.add_argument("--account", required=True, help="Expected AWS account ID")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.account or args.output.exists() or not args.certificate.is_file():
        raise RuntimeError("Confirm AWS account, public certificate, and an unused encrypted output path")
    stack = "reeldub-backup-v1"
    subprocess.run(["aws", "cloudformation", "deploy", "--region", args.region,
                    "--template-file", str(Path(__file__).with_name("aws.yaml")),
                    "--stack-name", stack, "--capabilities", "CAPABILITY_IAM",
                    "--no-fail-on-empty-changeset"], check=True)
    info = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in info["Outputs"]}
    iam = session.client("iam")
    result = {"region": args.region, "account": account, "bucket": outputs["Bucket"]}
    created = []
    try:
        for label, profile in [("SnapshotUser", "backup"), ("ReceiptUser", "erasure")]:
            username = outputs[label]
            if iam.list_access_keys(UserName=username)["AccessKeyMetadata"]:
                raise RuntimeError("Writer already has credentials. Use your existing encrypted bundle or rotate intentionally.")
            key = iam.create_access_key(UserName=username)["AccessKey"]
            created.append((username, key["AccessKeyId"]))
            result[profile] = {k: key[k] for k in ("AccessKeyId", "SecretAccessKey")}
        encrypted = subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER",
                                    str(args.certificate)], input=json.dumps(result).encode(),
                                   stdout=subprocess.PIPE, check=True).stdout
        with args.output.open("xb") as out:
            out.write(encrypted)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        for user, key_id in created:
            iam.delete_access_key(UserName=user, AccessKeyId=key_id)
        raise
    print("Private S3 archive is ready. Encrypted create-only credentials:", args.output)


if __name__ == "__main__":
    main()
