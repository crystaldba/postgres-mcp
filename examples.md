# Overview

Here are a few examples of of using [Postgres Pro](https://github.com/crystaldba/postgres-mcp) to build, test, and scale applications. Postgres Pro is an MCP server by [crystaldba.ai](crystaldba.ai) which gives apps like Cursor, Windsurf and others access to a Postgres expert using the [MCP protocol](https://modelcontextprotocol.io/introduction) from Anthropic.

# Examples

## Movie Critic App

Let's do a quick AI-coding session and take an idea from concept to launch!

We'll use the [IMDB dataset](https://developer.imdb.com/non-commercial-datasets/) to build a movie ratings website.

Our AI tools:
- **Replit** - for the initial prototype
- **Cursor** - as our AI coding agent
- **Postgres Pro** - to give Cursor a Postgres expert

Let's get started...

<table>
  <tbody>
    <tr>
      <td align="left" valign="top">
        <h4>1) Create the initial app on Replit</h4>
        <p>We prompt Replit with:</p>
        <blockquote>
          <p>Create a web app based on flask, python and SQAlchemy ORM</p>
          <p>It's website that uses the schema from the public IMDB dataset (<a href="https://developer.imdb.com/non-commercial-datasets/">https://developer.imdb.com/non-commercial-datasets/</a>). Assume I've imported the IMDB dataset as-is and add to that. I want people to be able to browse a mobile-friendly page for each movie, with all the IMDB data related to that movie. Additionally, people can rate each movie 1-5 and view top rated movies. The community and these ratings are one of the primary uses cases for the website.</p>
        </blockquote>
        <p><b>Boom!</b> We have a fully functionaly website with ratings, search, browse, auth -- in under an hour.  What!!  So cool.</p>
        <p>But it's slooooow...</p>
    </td>
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-0-initial-app.png"/></td>
    </tr>
    <tr>
      <td align="left" valign="top">
        <h4>2) Fix query performance</h4>
        <p>That was a thrill, but it can't handle even 1 user.</p>
        <p>Let's switch to Cursor w/ Postgres Pro to get the app ready for launch.</p>
      </td>
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-1-go-0-to-1.png"/></td>
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
      <td align="center"><a href="https://youtu.be/1yEPbP_Sve0"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-2-movie-details.png"/></a></td>
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
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-2-movie-details.png"/></td>
    </tr>
  </tbody>
</table>
