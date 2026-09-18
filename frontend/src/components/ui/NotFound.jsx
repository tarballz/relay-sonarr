import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Not found</h1>
      </div>
      <div className="panel pad-lg">
        <p className="muted">That page doesn’t exist.</p>
        <Link className="btn" to="/">
          Back to search
        </Link>
      </div>
    </>
  );
}
