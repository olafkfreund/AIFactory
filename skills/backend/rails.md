# rails

> Source: curated best practices | 2026

---

# Rails - Convention-driven Ruby web + API

This skill equips the coder to build production Ruby on Rails 7.x apps (Ruby 3.2+), including API-only mode. It enforces RESTful controllers, strong parameters, fat-model/skinny-controller with service objects for complex flows, ActiveRecord scopes and `includes` to avoid N+1, migrations for every schema change, `has_secure_password`/JWT or session auth, credentials for secrets, and request/model specs with RSpec or Minitest. Follows Rails conventions (naming, autoloading via Zeitwerk) rather than fighting them.

## When to Activate

Use when building with Rails:
- Building Ruby on Rails web apps or `--api` JSON services
- Files under `app/controllers`, `app/models`, `db/migrate`, or `config/routes.rb`
- Adding resources, migrations, ActiveRecord associations, or strong params
- Service objects, scopes, validations, or auth (Devise/`has_secure_password`/JWT)

## Patterns and Best Practices

Standard structure:

```
app/
  controllers/api/v1/users_controller.rb
  models/user.rb
  services/create_user.rb
  serializers/user_serializer.rb   # or jbuilder views
config/routes.rb
db/migrate/
spec/
```

Migration — always the way schema changes:

```ruby
# db/migrate/20260101000000_create_articles.rb
class CreateArticles < ActiveRecord::Migration[7.1]
  def change
    create_table :articles do |t|
      t.references :user, null: false, foreign_key: true
      t.string :title, null: false
      t.string :slug, null: false
      t.text :body
      t.datetime :published_at
      t.timestamps
    end
    add_index :articles, :slug, unique: true
  end
end
```

Model with validations, associations, and scopes:

```ruby
# app/models/article.rb
class Article < ApplicationRecord
  belongs_to :user
  validates :title, presence: true, length: { minimum: 3 }
  validates :slug, presence: true, uniqueness: true

  scope :published, -> { where.not(published_at: nil) }
  scope :recent, -> { order(created_at: :desc) }

  before_validation :set_slug, on: :create

  private

  def set_slug
    self.slug ||= title.to_s.parameterize
  end
end
```

Service object for multi-step business logic:

```ruby
# app/services/create_article.rb
class CreateArticle
  Result = Struct.new(:success?, :article, :errors)

  def self.call(user:, params:)
    article = user.articles.build(params)
    if article.save
      Result.new(true, article, nil)
    else
      Result.new(false, nil, article.errors.full_messages)
    end
  end
end
```

RESTful controller with strong params:

```ruby
# app/controllers/api/v1/articles_controller.rb
module Api
  module V1
    class ArticlesController < ApplicationController
      before_action :authenticate_user!
      before_action :set_article, only: %i[show update destroy]

      def index
        # includes(:user) prevents N+1 when serializing author
        articles = Article.includes(:user).recent.page(params[:page])
        render json: articles
      end

      def show
        render json: @article
      end

      def create
        result = CreateArticle.call(user: current_user, params: article_params)
        if result.success?
          render json: result.article, status: :created
        else
          render json: { errors: result.errors }, status: :unprocessable_entity
        end
      end

      private

      def set_article
        @article = Article.find(params[:id])
      end

      def article_params
        params.require(:article).permit(:title, :body)
      end
    end
  end
end
```

Routes:

```ruby
# config/routes.rb
Rails.application.routes.draw do
  namespace :api do
    namespace :v1 do
      resources :articles
      post "auth/login", to: "sessions#create"
    end
  end
end
```

Token auth with `has_secure_password`:

```ruby
# app/models/user.rb
class User < ApplicationRecord
  has_secure_password
  has_many :articles, dependent: :destroy
  validates :email, presence: true, uniqueness: true
end
```

```ruby
# app/controllers/api/v1/sessions_controller.rb
def create
  user = User.find_by(email: params[:email])
  if user&.authenticate(params[:password])
    token = JWT.encode({ sub: user.id, exp: 24.hours.from_now.to_i }, Rails.application.secret_key_base)
    render json: { token: token }
  else
    render json: { error: "invalid credentials" }, status: :unauthorized
  end
end
```

Global error handling via `rescue_from`:

```ruby
# app/controllers/application_controller.rb
class ApplicationController < ActionController::API
  rescue_from ActiveRecord::RecordNotFound do
    render json: { error: "not found" }, status: :not_found
  end
end
```

Secrets live in encrypted credentials, not source:

```ruby
Rails.application.credentials.dig(:aws, :access_key_id)
```

Request spec (RSpec):

```ruby
# spec/requests/articles_spec.rb
RSpec.describe "Articles", type: :request do
  let(:user) { User.create!(email: "a@b.com", password: "secret123") }

  it "rejects invalid title" do
    post "/api/v1/articles",
      params: { article: { title: "no", body: "x" } },
      headers: auth_headers(user)
    expect(response).to have_http_status(:unprocessable_entity)
  end
end
```

## Anti-patterns

- Fat controllers with business logic — extract to models or service objects.
- N+1 queries: rendering associated records without `includes`/`preload`.
- Skipping strong parameters or using `permit!` — mass-assignment vulnerability.
- Editing `db/schema.rb` by hand or changing columns without a migration.
- Callback soup (`before_save` chains with side effects) instead of explicit service calls.
- Storing secrets in `config/*.yml` in source instead of encrypted credentials / ENV.
- Fighting conventions: non-standard file names break Zeitwerk autoloading.
