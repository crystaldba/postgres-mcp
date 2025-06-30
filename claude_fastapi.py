#!/usr/bin/env python3

import asyncio
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json

from claude_mcp_client import ClaudeMCPClient

"""
postgres-mcp postgres://<balbablba>/db --transport sse --sse-port 8000
"""
app = FastAPI(
    title="Claude MCP API",
    description="Chat with Claude using PostgreSQL database tools via MCP",
    version="1.0.0"
)

# Global client instance
claude_client: Optional[ClaudeMCPClient] = None


class QuestionRequest(BaseModel):
    question: str
    stream: bool = False


class QuestionResponse(BaseModel):
    response: str
    tools_used: list = []


@app.on_event("startup")
async def startup_event():
    """Initialize Claude MCP client on startup."""
    global claude_client
    
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  Warning: ANTHROPIC_API_KEY not set. Claude functionality will be disabled.")
        return
    
    claude_client = ClaudeMCPClient(api_key)
    
    # Connect to MCP server
    success = await claude_client.connect_to_mcp()
    if success:
        print(f"✅ Connected to MCP server with {len(claude_client.available_tools)} tools")
    else:
        print("❌ Failed to connect to MCP server")
        claude_client = None


@app.get("/")
async def root():
    """Health check endpoint."""
    status = "connected" if claude_client else "disconnected"
    tools_count = len(claude_client.available_tools) if claude_client else 0
    
    return {
        "message": "Claude MCP API is running",
        "status": status,
        "tools_available": tools_count
    }


@app.get("/health")
async def health_check():
    """Detailed health check."""
    if not claude_client:
        raise HTTPException(status_code=503, detail="Claude client not initialized")
    
    return {
        "claude_connected": True,
        "mcp_tools": len(claude_client.available_tools),
        "available_tools": [tool["name"] for tool in claude_client.available_tools[:5]]  # Show first 5
    }


@app.post("/ask", response_model=QuestionResponse)
async def ask_claude(request: QuestionRequest):
    """Ask Claude a question about the database."""
    if not claude_client:
        raise HTTPException(status_code=503, detail="Claude client not available. Check ANTHROPIC_API_KEY.")
    
    try:
        response = await claude_client.ask_claude(request.question)
        
        return QuestionResponse(
            response=response,
            tools_used=[]  # Could track this if needed
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing question: {str(e)}")


@app.get("/ask-stream")
async def ask_claude_stream(question: str):
    """Ask Claude a question and stream the response."""
    if not claude_client:
        raise HTTPException(status_code=503, detail="Claude client not available")
    
    async def generate_stream():
        try:
            # Send initial status
            yield f"data: {json.dumps({'type': 'status', 'message': 'Processing question...'})}\n\n"
            
            # This is a simplified streaming - in a real implementation you'd 
            # need to modify the claude_client to yield intermediate results
            response = await claude_client.ask_claude(question)
            
            # Stream the final response
            yield f"data: {json.dumps({'type': 'response', 'content': response})}\n\n"
            yield f"data: {json.dumps({'type': 'complete'})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@app.get("/tools")
async def list_available_tools():
    """List all available MCP tools."""
    if not claude_client:
        raise HTTPException(status_code=503, detail="Claude client not available")
    
    return {
        "tools": claude_client.available_tools
    }


# Example questions endpoint for testing
@app.get("/examples")
async def get_example_questions():
    """Get example questions to ask Claude."""
    return {
        "examples": [
            "What schemas are available in the database?",
            "What tables can you see? Show me tables from all user schemas.",
            "How many patients are in the database?",
            "What are the most recent 5 tasks?",
            "Show me the structure of the Patient table",
            "What forms are available in the system?",
            "How many SMS campaigns are there?",
            "What providers are in the system?"
        ]
    }


if __name__ == "__main__":
    import uvicorn
    
    print("🚀 Starting Claude MCP FastAPI server...")
    print("📋 Available endpoints:")
    print("  - GET  / - Health check")
    print("  - POST /ask - Ask Claude a question")
    print("  - GET  /ask-stream?question=... - Stream Claude's response")
    print("  - GET  /tools - List available MCP tools")
    print("  - GET  /examples - Get example questions")
    print("  - GET  /docs - FastAPI documentation")
    print("\n💡 Make sure to set ANTHROPIC_API_KEY environment variable!")
    print("💡 Make sure MCP server is running on port 8000!")
    
    uvicorn.run(
        "claude_fastapi:app",
        host="localhost",
        port=8003,
        reload=True
    )