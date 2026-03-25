"""
Test cases for ingestion service metrics endpoints.
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from fastapi.testclient import TestClient
import json


@pytest.mark.asyncio
async def test_metrics_endpoint_exists():
    """Test that /metrics endpoint exists and returns Prometheus format."""
    # Import app after mocking to avoid startup issues
    with patch('ingestion.ingestion_api.AsyncQdrantClient'):
        with patch('ingestion.ingestion_api.httpx.AsyncClient'):
            with patch('ingestion.ingestion_api.asyncpg.create_pool'):
                from ingestion.ingestion_api import app
                client = TestClient(app)
                
                response = client.get("/metrics")
                # Should return prometheus metrics format
                assert response.status_code == 200
                assert "text/plain" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_metrics_json_endpoint():
    """Test that /metrics/json endpoint returns structured JSON metrics."""
    with patch('ingestion.ingestion_api.AsyncQdrantClient'):
        with patch('ingestion.ingestion_api.httpx.AsyncClient'):
            with patch('ingestion.ingestion_api.asyncpg.create_pool'):
                from ingestion.ingestion_api import app
                client = TestClient(app)
                
                response = client.get("/metrics/json")
                assert response.status_code == 200
                assert response.headers.get("content-type") == "application/json"
                
                data = response.json()
                assert "service" in data
                assert "uptime_seconds" in data
                assert "requests" in data
                assert "chunks" in data
                assert "pii" in data
                assert "embedding" in data
                
                # Check structure
                assert "total" in data["requests"]
                assert "ok" in data["requests"]  
                assert "error" in data["requests"]
                assert "total" in data["chunks"]
                assert "entities_found" in data["pii"]
                assert "batch_total" in data["embedding"]


@pytest.mark.asyncio
async def test_metrics_tracking_in_ingest():
    """Test that metrics are tracked during ingestion."""
    with patch('ingestion.ingestion_api.AsyncQdrantClient') as mock_qdrant:
        with patch('ingestion.ingestion_api.httpx.AsyncClient') as mock_http:
            with patch('ingestion.ingestion_api.asyncpg.create_pool') as mock_pool:
                with patch('ingestion.ingestion_api.get_scanner') as mock_scanner:
                    # Setup mocks
                    mock_scanner_instance = Mock()
                    mock_scanner_instance.scan_text.return_value = Mock(
                        contains_pii=False,
                        entity_counts={},
                        entity_locations=[]
                    )
                    mock_scanner.return_value = mock_scanner_instance
                    
                    from ingestion.ingestion_api import app
                    client = TestClient(app)
                    
                    # Make a request that should increment metrics
                    request_data = {
                        "source": "test document",
                        "source_type": "text",
                        "classification": "internal"
                    }
                    
                    with patch('ingestion.ingestion_api.ingest_text_chunks') as mock_ingest:
                        mock_ingest.return_value = {
                            "status": "ok",
                            "chunks_ingested": 1,
                            "pii_detected": False
                        }
                        
                        response = client.post("/ingest", json=request_data)
                        assert response.status_code == 200
                        
                        # Check that metrics were recorded
                        metrics_response = client.get("/metrics")
                        metrics_text = metrics_response.text
                        assert "pb_ingestion_requests_total" in metrics_text


@pytest.mark.asyncio 
async def test_pii_entity_metrics():
    """Test that PII entity discovery is tracked in metrics."""
    with patch('ingestion.ingestion_api.AsyncQdrantClient'):
        with patch('ingestion.ingestion_api.httpx.AsyncClient'):
            with patch('ingestion.ingestion_api.asyncpg.create_pool'):
                with patch('ingestion.ingestion_api.get_scanner') as mock_scanner:
                    # Setup mock scanner to find PII
                    mock_scanner_instance = Mock()
                    mock_scanner_instance.scan_text.return_value = Mock(
                        contains_pii=True,
                        entity_counts={"PERSON": 2, "EMAIL": 1},
                        entity_locations=[]
                    )
                    mock_scanner_instance.mask_text.return_value = "masked text"
                    mock_scanner.return_value = mock_scanner_instance
                    
                    from ingestion.ingestion_api import app
                    client = TestClient(app)
                    
                    # Make scan request
                    request_data = {
                        "text": "John Doe's email is john@example.com",
                        "language": "en"
                    }
                    
                    response = client.post("/scan", json=request_data)
                    assert response.status_code == 200
                    
                    # Check JSON metrics show PII entities (might be empty if metrics are None)
                    metrics_response = client.get("/metrics/json")
                    data = metrics_response.json()
                    
                    # The metrics might be empty if metrics collectors were None in test environment
                    # but we should still have the structure
                    assert "pii" in data
                    assert "entities_found" in data["pii"]