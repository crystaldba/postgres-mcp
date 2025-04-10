# PostgreSQL MCP Demo Scripts

This directory contains setup scripts for the PostgreSQL MCP Server demos. Each script creates a specialized database with particular characteristics to demonstrate different capabilities of PostgreSQL MCP.

## Setup Requirements

Before running these scripts, make sure you have:

1. PostgreSQL installed and running (version 12 or higher recommended)
2. Superuser privileges to create databases and install extensions
3. At least 4GB of free RAM and 5GB of free disk space for basic demos
4. At least 8GB of free RAM and 10GB of free disk space for advanced demos

## Quick Start

The easiest way to run the demos is using the provided shell script:

```bash
# Make the script executable
chmod +x run_demo.sh

# Show available demos
./run_demo.sh --list

# Run a specific demo (e.g., Demo 1: Database Health Assessment)
./run_demo.sh 1

# Clean up all demo databases when finished
./run_demo.sh --cleanup
```

## Basic Demos

The basic demos are lighter-weight and focus on specific PostgreSQL MCP capabilities:

### Demo 1: Database Health Assessment
- Demonstrates identifying and fixing common database health issues
- Creates tables with bloat, missing indexes, depleted sequences, etc.
- Usage: `./run_demo.sh 1`

### Demo 2: Index Recommendations for a Slow Application
- Demonstrates query analysis and index recommendations
- Creates a reporting application database with missing indexes
- Usage: `./run_demo.sh 2`

### Demo 3: Query Optimization with Hypothetical Indexes
- Demonstrates testing index improvements without creating them
- Creates an e-commerce database with complex joins that need optimization
- Usage: `./run_demo.sh 3`

### Demo 4: Schema Discovery and Documentation
- Demonstrates exploring complex database schemas
- Creates multiple schemas with relationships, views, and functions
- Usage: `./run_demo.sh 4`

### Demo 5: Storage Optimization
- Demonstrates reducing database size and improving storage efficiency
- Creates tables with redundant indexes, bloat, and suboptimal data types
- Usage: `./run_demo.sh 5`

### Demo 6: Advanced Workload Analysis
- Demonstrates analyzing query patterns for scaling and optimization
- Creates a database with realistic production workload patterns
- Usage: `./run_demo.sh 6`

## Advanced Multi-Step Demos

The advanced demos are more resource-intensive and demonstrate complex scenarios through multi-step interactions:

### Demo 7: E-commerce Performance Optimization
- Optimizes a growing e-commerce platform experiencing slowdowns
- Demonstrates comprehensive performance tuning
- Usage: `./run_demo.sh 7`

### Demo 8: Data Warehouse Query Optimization
- Speeds up slow analytical queries in a data warehouse
- Demonstrates materialized views, partitioning, and indexing strategies
- Usage: `./run_demo.sh 8`

### Demo 9: Database Migration Validation
- Verifies and optimizes a database after migration from MySQL
- Demonstrates identifying and fixing common migration issues
- Usage: `./run_demo.sh 9`

### Demo 10: Time-Series Data Management
- Optimizes an IoT database with high-volume sensor data
- Demonstrates TimescaleDB integration and time-series optimizations
- Usage: `./run_demo.sh 10`

## Resource Considerations

These scripts create databases with:
- Large numbers of tables and rows
- Multiple indexes 
- Complex relationships
- Intentional performance issues (for demonstration purposes)

Each database requires significant resources:
- **Basic demos**: ~1GB disk space per demo, 1GB+ RAM
- **E-commerce demo**: ~5GB disk space, 2GB+ RAM
- **Data warehouse demo**: ~8GB disk space, 4GB+ RAM
- **Migration validation demo**: ~2GB disk space, 1GB+ RAM
- **Time-series IoT demo**: ~3GB disk space, 2GB+ RAM

If you are running on a system with limited resources, you can modify the scripts to reduce the data volume by changing the number of rows generated in the `generate_series()` calls.

## Cleanup

After you're finished with the demos, you can remove the databases with:

```bash
# Clean up all demo databases
./run_demo.sh --cleanup

# Or drop databases manually
dropdb mcp_demo_health
dropdb mcp_demo_slow_app
# etc.
```

## Advanced Usage

### Customizing the Demos

To modify the demos:

1. Edit the SQL files in the `demo_scripts/` directory
3. Reduce the data volume by modifying `generate_series()` parameters

### Using Specific Parts of Demos

If you want to use specific parts of a demo:

1. Extract the relevant SQL statements from the setup file
2. Create a custom database and run just those statements
3. Use the MCP agent to analyze those specific aspects
