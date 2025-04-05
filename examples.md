## Overview

Here are a few examples of of using [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) by [crystaldba.ai](crystaldba.ai) to build, test, and scale your application.  It works in any MCP client such as... **(TODO)**

## Examples

### Movie Critic App

Let's do a quick AI-coding session and take an idea from concept to launch!

OK. Let's build the best movie ratings community/app. We'll use the [IMDB dataset](https://developer.imdb.com/non-commercial-datasets/) to seed our database.

#### Stage 1: replit-induced joy

On [replit.com](https://replit.com/) we enter...
> Create a web app based on flask, python and SQAlchemy ORM
> It's website that uses the schema from the public IMDB dataset (https://developer.imdb.com/non-commercial-datasets/ ). Assume I've imported the IMDB dataset as-is and add to that. I want people to be able to browse a mobile-friendly page for each movie, with all the IMDB data related to that movie. Additionally, people can rate each movie 1-5 and view top rated movies. The community and these ratings are one of the primary uses cases for the website.

**Boom!** We have a fully functionaly website with ratings, search, browse, auth -- in under 30 minutes.  What!  Am I officially a vibe-coder now?

This is amazing.  But it's sooooo slow!

![Movie Critic App](https://deploy-preview-152--elated-shockley-6a4090.netlify.app/screenshots/mcp/mc-0-initial-app.gif "Intiial Movie Critic app - so slow!")

#### Stage 2: go 0->1...person

I built that fast, but it won't even work for 1 person.  Let's get the app ready for launch. 

We'll switch to Cursor and install [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp)


| title  | title  |
| :--- | :---: |
| what? asdfasdfas dfasd fadsf asdf asdfajksdhflajksdhfasdf asf asdf asdfa | ![Movie Critic App](https://deploy-preview-152--elated-shockley-6a4090.netlify.app/screenshots/mcp/mc-0-initial-app.gif "Intiial Movie Critic app - so slow!")  |
|   |   |
|   |   |


| - | - |
| **Text**: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum | **Text** Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum |




<div align="center">
  some textasdf asdf asf asdfasd asdf asdf asd asdfasdf adsfasdf asdf asdf as dfads fasdf
  asdf adsf asdf asdf as
  df as fdsaf 
  <a href="https://github.com/testl">
    <img src="https://deploy-preview-152--elated-shockley-6a4090.netlify.app/screenshots/mcp/mc-0-initial-app.gif" width="412px"/>
  </a>
</div>