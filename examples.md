# Overview

Here are a few examples of of using [Postgres Pro](https://github.com/crystaldba/postgres-mcp), an MCP server by [crystaldba.ai](crystaldba.ai), to build, test, and scale applications. 

# Examples

## Movie Critic App

Let's do a quick AI-coding session and take an idea from concept to launch!

We'll use the [IMDB dataset](https://developer.imdb.com/non-commercial-datasets/) to build a movie ratings website.

Our AI tools:
- **Replit** - for the initial prototype
- **Cursor** - as our AI coding agent
- **Postgres Pro** - to give Cursor a Postgres expert

<table>
  <tbody>
    <tr>
      <td align="left">
        <h4>1) Create the initial app on Replit</h4>
        <p>On Replit we enter...</p>
        <blockquote>
          <p>Create a web app based on flask, python and SQAlchemy ORM</p>
          <p>It's website that uses the schema from the public IMDB dataset (<a href="https://developer.imdb.com/non-commercial-datasets/">https://developer.imdb.com/non-commercial-datasets/</a>). Assume I've imported the IMDB dataset as-is and add to that. I want people to be able to browse a mobile-friendly page for each movie, with all the IMDB data related to that movie. Additionally, people can rate each movie 1-5 and view top rated movies. The community and these ratings are one of the primary uses cases for the website.</p>
        </blockquote>
        <p><b>Boom!</b> We have a fully functionaly website with ratings, search, browse, auth -- in under 30 minutes.  What!!  So cool.</p>
        <p>But it's sooooo slow...</p>
    </td>
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-0-initial-app.png"/></td>
    </tr>
    <tr>
      <td align="left">
        <h4>2) Fix query performance</h4>
        <p>I built that fast, but it won't even work for 1 user. Let's get the app ready for launch.
        <p>We'll switch to Cursor and install [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp)</p>
      </td>
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-2-movie-details.png"/></td>
    </tr>
    <tr>
      <td align="left">
        <h4>3) Fix empty movie details pages</h4>
        <p>On Replit we enter...</p>
      </td>
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-2-movie-details.png"/></td>
    </tr>
    <tr>
      <td align="left">
        <h4>4) Improve the top-rated sorting</h4>
        <p>On Replit we enter...</p>
      </td>
      <td align="center"><img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/demos/mc-2-movie-details.png"/></td>
    </tr>
  </tbody>
</table>
