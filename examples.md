# Overview

Here are a few examples of of using [Postgres Pro](https://github.com/crystaldba/postgres-mcp) to build, test, and scale applications. Postgres Pro is an MCP server by [crystaldba.ai](crystaldba.ai) which gives apps like Cursor, Windsurf and others access to a Postgres expert using the [MCP protocol](https://modelcontextprotocol.io/introduction) from Anthropic.

# Examples

## Movie Ratings Website

Let's do a quick AI-coding session and take an idea from concept to launch!

We'll use the [IMDB dataset](https://developer.imdb.com/non-commercial-datasets/) to build a movie ratings website.

Our AI tools:
- **Replit** - for the initial prototype
- **Cursor** - as our AI coding agent
- **Postgres Pro** - to give Cursor a Postgres expert

What we did:
1) Create the initial app on Replit
2) Fix query performance
3) Fix empty movie details pages
4) Improve the sort for top-rated movies

**Let's get started...**

<table>
  <tbody>
    <tr>
      <td align="left" valign="top">
        <h4>1) Create the initial app on Replit</h4>
        <p>We prompt Replit with:</p>
        <blockquote>
          <p>Create a web app based on flask, python and SQAlchemy ORM</p>
          <p>It's website that uses the schema from the public IMDB dataset . Assume I've imported the IMDB dataset as-is and add to that. I want people to be able to browse a mobile-friendly page for each movie, with all the IMDB data related to that movie. Additionally, people can rate each movie 1-5 and view top rated movies. The community and these ratings are one of the primary uses cases for the website.</p>
        </blockquote>
        <p><b>Boom!</b> We have a fully functionaly website with ratings, search, browse, auth -- in under an hour.  What!!  So cool.</p>
        <p>But it's slooooow...</p>
    </td>
      <td align="center"><img src="https://github.com/user-attachments/assets/2609dfcb-2ff3-45b9-89f1-6d991e65c461"/></td>
    </tr>
    <tr>
      <td align="left" valign="top">
        <h4>2) Fix query performance</h4>
        <p>Our website looks decent, but it's too slow to ship.<br/>
        Let's switch to Cursor w/ Postgres Pro to get the app ready for launch.</p>
        <p>Our prompt:</p>
        <blockquote>
          <p>My app is slow!  Look for opportunities with poor queries, bad indexes, or caching.</p>
          <div>1. Look at code to figure out all the queries for all routes and models</div>
          <div>2. Analyze the explain plans and identify what indexes might help</div>
          <div>3. Test the indexes by using the explain plans with hypothetical indexes.</div>
          <div>4. Compare your list to what is already created to finalize a list to both add and remove</div>
          <div>5. Create a migration script using alembic but don't apply it yet</div>
          <div>6. Make all code changes necessary for queries, indexes, and caching</div>
        </blockquote>
        <div><em>(7 minutes later...)</em></div>
        <p>Let's see what all the AI agent did.</p>
        <ol>
          <li>Explored the schema and code to identify potential problem queries.</li>
          <li>Used Postgres Pro to help identify solutions, calling tools like <code>explain_plan</code>, <code>get_top_queries</code>, <code>analyze_query_indexes</code>, <code>analyze_database_health</code>.</li>
          <li>Added indexes to fix table scans and ILIKE queries</li>
          <li>Remove unused and bloated indexes</li>
          <li>Optimized complex sub-queries causing repeated database hits</li>
          <li>Added caching for image loading and expensive queries</li>
          <li>Created an alembic migration script to apply the changes.</li>
        </ol>
        <p>That was amazing! I was running in "yolo" mode in Cursor, so it did all that without my input. I had Postgres Pro in "restricted" mode (read-only), so I did not have to worry about unintended database changes.</p>
      </td>
      <td align="center"><a href="https://youtu.be/qhcqZ6Lxg3c"><img src="https://github.com/user-attachments/assets/3e9cdd1d-e93e-4e4a-a043-ffdc6f4feea6"/></a></td>
    </tr>
    <tr>
      <td align="left" valign="top">
        <h4>3) Fix empty movie details pages</h4>
        <p>The movie details looks empty. Let's investigate.</p>
        <blockquote>
          <div>The movie details page looks awful.</div>
          <div>- no cast/crew. Are we missing the data or is the query wrong?</div>
          <div>- The ratings looks misplaced. move it closer to the title</div>
          <div>- Do we have additional data we can include like a description? Check the schema.</div>
        </blockquote>
        <div>The result?</div>
        <ol>
          <li>It used Postgres Pro to inspect the schema and compare it against the code.</li>
          <li>It fixed the query in the route to join with <code>name_basics</code>.</li>
          <li>It identified additional data in <code>title_basics</code>
          to create a new About section with genre, runtime, and release years.</li>
        </ol>
        <p>Am I missing any data?</p>
        <p>The AI Agent ran the sql queries via Postgres Pro, found the missing data, and wrote a script
        to import them in a more reliable way.</p>
        <div><em>(it turned out my original script aborted on errors)</em></p>
      </td>
      <td align="center"><a href="https://youtu.be/1yEPbP_Sve0"><img src="https://github.com/user-attachments/assets/78b9df86-c9ae-4cc1-98c8-1090bd0c8193"/></a></td>
    </tr>
    <tr>
      <td align="left" valign="top">
        <h4>4) Improve the sort for top-rated movies</h4>
        <p>I haven't heard of movies like "Brothers", "Carraco", etc. in the top-rated page. Something is wrong:</p>
        <blockquote>
          <div>How are the top-rated sorted?  It seems random.
          Do we have data in those tables?  Is the query it uses working?</div>
        </blockquote>
        <div>The Agent queries Postgres and identifies we need to add a minimum on <code>num_votes</code></div>
        <br/>
        <div>So I ask:</div>
        <blockquote>
          <div>help me find a good minimum of reviews</div>
        </blockquote>
        <div>The AI Agent pulls sample data via the Postgres MCP to determine that a 10K vote minimum would work.</div>
        <div>It gives me confidence seeing the results are grounded in reality and not just some hallucination.</div>
      </td>
      <td align="center"><a href="https://youtube.com/shorts/UTqmeiC2xU8"><img src="https://github.com/user-attachments/assets/7e2a82a4-dc5c-4c1e-89fe-8d8061fb2af9"/></a></td>
    </tr>
  </tbody>
</table>


