#!/usr/bin/env python3

import asyncio
import json
import os
from typing import Any, Dict, List

from mcp.client.sse import sse_client
from mcp import ClientSession
import anthropic


class ClaudeMCPClient:
    def __init__(self, anthropic_api_key: str):
        self.anthropic_client = anthropic.Anthropic(
            api_key=anthropic_api_key
        )
        self.mcp_session = None
        self.available_tools = []
        self.sql_tool_name = None
        
    async def connect_to_mcp(self):
        """Connect to the MCP server and get available tools."""
        try:
            print("🔗 Connecting to MCP server...")
            
            # Note: In a real implementation, you'd want to maintain this connection
            # For now, we'll create it fresh each time we need it
            async with sse_client("http://localhost:8000/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    
                    self.available_tools = []
                    for tool in tools.tools:
                        self.available_tools.append({
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": []
                            }
                        })
                        
                        # Find the SQL execution tool
                        if "execute_sql" in tool.name:
                            self.sql_tool_name = tool.name
                    
                    print(f"✅ Connected to MCP! Found {len(self.available_tools)} tools")
                    return True
                    
        except Exception as e:
            print(f"❌ Failed to connect to MCP: {e}")
            return False
    
    async def execute_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool via MCP and return the result as a string."""
        try:
            async with sse_client("http://localhost:8000/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    
                    # Extract text content from the result
                    if result.content:
                        return "\n".join([
                            content.text for content in result.content 
                            if hasattr(content, 'text')
                        ])
                    else:
                        return "No results returned"
                        
        except Exception as e:
            return f"Error executing tool: {e}"
    
    def create_claude_tools(self) -> List[Dict]:
        """Convert MCP tools to Claude API tool format."""
        claude_tools = []
        
        for tool in self.available_tools:
            if "execute_sql" in tool["name"]:
                claude_tools.append({
                    "name": "execute_sql",
                    "description": "Execute SQL queries against the PostgreSQL database",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "The SQL query to execute"
                            }
                        },
                        "required": ["sql"]
                    }
                })
            elif "list_schemas" in tool["name"]:
                claude_tools.append({
                    "name": "list_schemas",
                    "description": "List all database schemas",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                })
            elif "list_objects" in tool["name"]:
                claude_tools.append({
                    "name": "list_objects",
                    "description": "List database objects (tables, views, etc.) in a schema",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "schema_name": {
                                "type": "string",
                                "description": "Name of the schema"
                            },
                            "object_type": {
                                "type": "string",
                                "description": "Type of object: table, view, sequence, or extension",
                                "default": "table"
                            }
                        },
                        "required": ["schema_name"]
                    }
                })
        
        return claude_tools
    
    async def ask_claude(self, question: str) -> str:
        """Ask Claude a question with access to MCP tools."""
        
        # First, ensure we're connected to MCP
        if not self.available_tools:
            await self.connect_to_mcp()
        
        claude_tools = self.create_claude_tools()
        
        messages = [
            {
                "role": "user",
                "content": question
            }
        ]
        
        try:
            # Continue conversation until Claude stops making tool calls
            max_iterations = 10  # Prevent infinite loops
            iteration = 0
            
            while iteration < max_iterations:
                iteration += 1
                print(f"🔄 Conversation iteration {iteration}")
                
                response = self.anthropic_client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    messages=messages,
                    tools=claude_tools if claude_tools else None,
                    max_tokens=4000
                )
                
                # Check if Claude wants to use tools
                tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
                
                if tool_use_blocks:
                    # Execute all requested tools
                    tool_results = []
                    
                    for block in tool_use_blocks:
                        tool_name = block.name
                        arguments = block.input
                        
                        print(f"🔧 Claude is calling tool: {tool_name}")
                        print(f"📝 Arguments: {arguments}")
                        
                        # Map Claude tool names to MCP tool names
                        mcp_tool_name = None
                        for mcp_tool in self.available_tools:
                            if tool_name in mcp_tool["name"] or mcp_tool["name"].endswith(f"_{tool_name}"):
                                mcp_tool_name = mcp_tool["name"]
                                break
                        
                        if mcp_tool_name:
                            result = await self.execute_mcp_tool(mcp_tool_name, arguments)
                            print(f"📊 Tool result: {result[:200]}...")
                            
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result
                            })
                        else:
                            tool_results.append({
                                "type": "tool_result", 
                                "tool_use_id": block.id,
                                "content": f"Error: Tool {tool_name} not found"
                            })
                    
                    # Add assistant response and tool results to conversation
                    messages.append({
                        "role": "assistant", 
                        "content": response.content
                    })
                    messages.append({
                        "role": "user",
                        "content": tool_results
                    })
                    
                    # Continue the loop to let Claude process the results and potentially make more tool calls
                    
                else:
                    # No more tool calls, Claude has finished
                    return response.content[0].text if response.content else "No response"
            
            # If we hit max iterations, return the last response
            return "Response truncated due to maximum iteration limit"
            
        except Exception as e:
            return f"Error calling Claude API: {e}"


async def main():
    """Interactive chat with Claude using MCP tools."""
    
    # Get Anthropic API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ Please set ANTHROPIC_API_KEY environment variable")
        return
    
    client = ClaudeMCPClient(api_key)
    
    # Connect to MCP
    if not await client.connect_to_mcp():
        print("❌ Failed to connect to MCP server. Make sure it's running on port 8000.")
        return
    
    print("\n🤖 Claude MCP Client Ready!")
    print("You can now ask Claude questions about your database.")
    print("Type 'quit' to exit.\n")
    
    while True:
        question = input("You: ").strip()
        
        if question.lower() in ['quit', 'exit', 'q']:
            print("👋 Goodbye!")
            break
        
        if not question:
            continue
        
        print("🤔 Claude is thinking...")
        response = await client.ask_claude(question)
        print(f"\n🤖 Claude: {response}\n")


if __name__ == "__main__":
    asyncio.run(main())