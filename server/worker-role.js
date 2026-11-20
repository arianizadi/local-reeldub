// Run as a database administrator. The worker credential is supplied through the environment.
const queue = db.getSiblingDB("reeldub");
const password = process.env.REELDUB_WORKER_PASSWORD;
if (!password || password.length < 32) throw Error("A separate worker password is required");
const name = "reeldubQueueWorker";
const privileges = [
  {resource: {db: "reeldub", collection: "queue_wake"}, actions: ["find"]},
  {resource: {db: "reeldub", collection: "jobs"}, actions: ["find", "update"]},
  ...["fs.files", "fs.chunks"].map(collection => ({
    resource: {db: "reeldub", collection},
    actions: ["find", "insert", "remove", "listIndexes", "createIndex"]
  }))
];
if (queue.getRole(name)) queue.updateRole(name, {privileges, roles: []});
else queue.createRole({role: name, privileges, roles: []});
if (queue.getUser("worker")) queue.updateUser("worker", {pwd: password, roles: [name]});
else queue.createUser({user: "worker", pwd: password, roles: [name]});
