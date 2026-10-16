"""The CP3 preview: each company's latest stored document per source, with links to click."""

from sqlalchemy import Engine, text

_LATEST = text(
    """
    SELECT DISTINCT ON (c.cik, split_part(d.source_key, ':', 1))
           c.name, c.cik, split_part(d.source_key, ':', 1) AS source,
           d.document_type, d.published_at, d.url
    FROM company c
    JOIN company_document cd ON cd.company_id = c.id
    JOIN source_document d ON d.id = cd.source_document_id
    ORDER BY c.cik, split_part(d.source_key, ':', 1), d.published_at DESC
    """
)
_UNLINKED = text(
    """
    SELECT DISTINCT ON (split_part(d.source_key, ':', 1), d.publisher)
           d.publisher, d.document_type, d.published_at, d.url
    FROM source_document d
    WHERE NOT EXISTS (SELECT 1 FROM company_document cd WHERE cd.source_document_id = d.id)
    ORDER BY split_part(d.source_key, ':', 1), d.publisher, d.published_at DESC
    """
)
_COUNTS = text(
    "SELECT split_part(source_key, ':', 1) AS source, count(*) FROM source_document GROUP BY 1 ORDER BY 1"
)
SOURCE_NAMES = {"sec": "SEC filing", "ctgov": "Trial registry", "openfda": "FDA application"}


def latest_documents_report(engine: Engine) -> str:
    """Return Markdown tables of the latest document per company and source, and per source-wide feed."""
    lines = [
        "| Company | Source | Document | Published (UTC) | Link |",
        "| --- | --- | --- | --- | --- |",
    ]
    with engine.connect() as connection:
        for row in connection.execute(_LATEST):
            published = row.published_at.strftime("%Y-%m-%d %H:%M")
            source = SOURCE_NAMES.get(row.source, row.source)
            lines.append(f"| {row.name} | {source} | {row.document_type} | {published} | [open]({row.url}) |")
        lines += ["", "| Source | Document | Published (UTC) | Link |", "| --- | --- | --- | --- |"]
        for row in connection.execute(_UNLINKED):
            published = row.published_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"| {row.publisher} | {row.document_type} | {published} | [open]({row.url}) |")
        lines += ["", "| Source | Documents stored |", "| --- | --- |"]
        lines += [f"| {row.source} | {row.count} |" for row in connection.execute(_COUNTS)]
    return "\n".join(lines)
