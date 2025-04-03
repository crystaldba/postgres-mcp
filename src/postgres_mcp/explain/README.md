# PostgreSQL Explain Tools

This module provides tools for analyzing PostgreSQL query execution plans and query structure.

## Tools

### ExplainPlanTool

Provides methods for generating different types of EXPLAIN plans:

- `explain()` - Basic EXPLAIN plan showing the execution plan and estimated costs
- `explain_analyze()` - EXPLAIN ANALYZE plan that actually runs the query and shows real execution statistics
- `explain_with_hypothetical_indexes()` - Explains how a query would perform with indexes that don't exist in the database

### SqlParserTool

Parses SQL queries and returns their abstract syntax tree (AST) in JSON format, which can be used to analyze query patterns, table relationships, and query structure.

### QueryPostgreSQLTool 

A tool for running read-only SQL queries against PostgreSQL.

## Usage

These tools are integrated into the PostgreSQL MCP server and can be used through the MCP API via the following functions:

- `explain_query`
- `explain_analyze_query`
- `explain_with_hypothetical_indexes`
- `parse_sql_query`

## Benefits

- **Query Understanding**: Helps understand how PostgreSQL executes queries
- **Performance Analysis**: Identifies bottlenecks and optimization opportunities
- **Index Testing**: Tests hypothetical indexes without actually creating them
- **Query Structure Analysis**: Analyzes query patterns and table relationships 
