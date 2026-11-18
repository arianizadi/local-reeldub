db.getSiblingDB("reeldub").createUser({
  user: "reeldub",
  pwd: process.env.REELDUB_DB_PASSWORD,
  roles: [{ role: "readWrite", db: "reeldub" }]
});
