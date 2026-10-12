import argparse
import json
from pathlib import Path
from .analyze import analyze
from .catalog import extract
from .query import capture


def main():
    parser = argparse.ArgumentParser(description='Evidence-preserving NGFW telemetry analysis')
    sub = parser.add_subparsers(dest='command', required=True)
    cat = sub.add_parser('catalog')
    cat.add_argument('dashboard')
    cat.add_argument('--source-url')
    cat.add_argument('--native-interval', type=float)
    cat.add_argument('--output', required=True)
    ana = sub.add_parser('analyze')
    ana.add_argument('manifest')
    ana.add_argument('--output', required=True)
    qry = sub.add_parser('query')
    qry.add_argument('--endpoint', required=True)
    qry.add_argument('--version', required=True, choices=['1', '2'])
    qry.add_argument('--query-file', required=True)
    qry.add_argument('--database')
    qry.add_argument('--org')
    qry.add_argument('--credential-env')
    qry.add_argument('--username')
    qry.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'catalog':
        result = extract(args.dashboard, args.source_url, args.native_interval)
    elif args.command == 'analyze':
        result = analyze(json.loads(Path(args.manifest).read_text(encoding='utf-8')))
    else:
        capture(args.output, args.endpoint, args.version, Path(args.query_file).read_text(encoding='utf-8'),
                database=args.database, org=args.org, credential_env=args.credential_env, username=args.username)
        return
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
