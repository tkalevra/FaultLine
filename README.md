# FaultLine

A write-validated personal knowledge graph pipeline for OpenWebUI. Extracts entities and relationships from conversation, validates them against an ontology, and persists them to PostgreSQL with Qdrant vector indexing for semantic memory recall.

## Quick Start

```bash
# Clone repo
git clone https://github.com/your-org/FaultLine.git
cd FaultLine

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Deploy with Docker
docker compose up -d
```

## Documentation

See **[ABOUT.md](ABOUT.md)** for full documentation:
- Architecture overview
- Features list
- Configuration reference
- API endpoint documentation
- Deployment guide

## License

MIT — see [LICENSE](LICENSE) for details.
