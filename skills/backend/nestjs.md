# nestjs

> Source: curated best practices | 2026

---

# NestJS - Structured TypeScript backend framework

This skill equips the coder to build production NestJS 10 applications on Node 20+ with TypeScript. It enforces the module/controller/provider structure, dependency injection, DTOs validated by `class-validator` + a global `ValidationPipe`, TypeORM or Prisma for persistence, Passport/JWT guards for auth, exception filters mapping to HTTP responses, `ConfigModule` for env, and `@nestjs/testing` unit/e2e tests with `supertest`. Assumes decorators and `emitDecoratorMetadata` enabled.

## When to Activate

Use when building with NestJS:
- Building NestJS APIs or microservices in TypeScript
- Files with `@Module`, `@Controller`, `@Injectable`, or `nestjs` imports
- Adding modules, providers, DTOs with `class-validator`, guards, pipes, or interceptors
- TypeORM/Prisma repositories, JWT auth guards, or exception filters

## Patterns and Best Practices

Feature-module structure:

```
src/
  main.ts
  app.module.ts
  users/
    users.module.ts users.controller.ts users.service.ts
    dto/create-user.dto.ts
    entities/user.entity.ts
  common/filters/http-exception.filter.ts
test/
  users.e2e-spec.ts
```

Bootstrap with a global validation pipe:

```ts
// main.ts
import { NestFactory } from "@nestjs/core";
import { ValidationPipe } from "@nestjs/common";
import { AppModule } from "./app.module.js";

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.useGlobalPipes(new ValidationPipe({ whitelist: true, transform: true }));
  await app.listen(process.env.PORT ?? 3000);
}
bootstrap();
```

Config via `ConfigModule` (validated, injectable):

```ts
// app.module.ts
import { Module } from "@nestjs/common";
import { ConfigModule } from "@nestjs/config";
import { UsersModule } from "./users/users.module.js";

@Module({
  imports: [ConfigModule.forRoot({ isGlobal: true }), UsersModule],
})
export class AppModule {}
```

DTO validated by decorators:

```ts
// users/dto/create-user.dto.ts
import { IsEmail, MinLength } from "class-validator";

export class CreateUserDto {
  @IsEmail()
  email!: string;

  @MinLength(8)
  password!: string;
}
```

Entity (TypeORM):

```ts
// users/entities/user.entity.ts
import { Column, Entity, Index, PrimaryGeneratedColumn } from "typeorm";

@Entity("users")
export class User {
  @PrimaryGeneratedColumn() id!: number;
  @Index({ unique: true }) @Column() email!: string;
  @Column() hashedPassword!: string;
}
```

Service holds business logic; repository injected:

```ts
// users/users.service.ts
import { ConflictException, Injectable } from "@nestjs/common";
import { InjectRepository } from "@nestjs/typeorm";
import { Repository } from "typeorm";
import { User } from "./entities/user.entity.js";
import { CreateUserDto } from "./dto/create-user.dto.js";

@Injectable()
export class UsersService {
  constructor(@InjectRepository(User) private readonly repo: Repository<User>) {}

  async create(dto: CreateUserDto): Promise<User> {
    if (await this.repo.exists({ where: { email: dto.email } }))
      throw new ConflictException("email already registered");
    const user = this.repo.create({ email: dto.email, hashedPassword: hash(dto.password) });
    return this.repo.save(user);
  }
}
```

Controller — thin, delegates to the service:

```ts
// users/users.controller.ts
import { Body, Controller, Get, Param, Post, UseGuards } from "@nestjs/common";
import { UsersService } from "./users.service.js";
import { CreateUserDto } from "./dto/create-user.dto.js";
import { JwtAuthGuard } from "../common/guards/jwt-auth.guard.js";

@Controller("users")
export class UsersController {
  constructor(private readonly service: UsersService) {}

  @Post()
  create(@Body() dto: CreateUserDto) {
    return this.service.create(dto);
  }

  @UseGuards(JwtAuthGuard)
  @Get(":id")
  get(@Param("id") id: string) {
    return this.service.findOne(+id);
  }
}
```

JWT guard via Passport:

```ts
// common/guards/jwt-auth.guard.ts
import { Injectable } from "@nestjs/common";
import { AuthGuard } from "@nestjs/passport";

@Injectable()
export class JwtAuthGuard extends AuthGuard("jwt") {}
```

Exception filter for consistent error bodies:

```ts
// common/filters/http-exception.filter.ts
import { ArgumentsHost, Catch, ExceptionFilter, HttpException } from "@nestjs/common";
import { Response } from "express";

@Catch(HttpException)
export class HttpExceptionFilter implements ExceptionFilter {
  catch(exc: HttpException, host: ArgumentsHost) {
    const res = host.switchToHttp().getResponse<Response>();
    const status = exc.getStatus();
    res.status(status).json({ statusCode: status, message: exc.message });
  }
}
```

e2e test with the testing module:

```ts
// test/users.e2e-spec.ts
import { Test } from "@nestjs/testing";
import { INestApplication, ValidationPipe } from "@nestjs/common";
import request from "supertest";
import { AppModule } from "../src/app.module.js";

describe("Users (e2e)", () => {
  let app: INestApplication;
  beforeAll(async () => {
    const mod = await Test.createTestingModule({ imports: [AppModule] }).compile();
    app = mod.createNestApplication();
    app.useGlobalPipes(new ValidationPipe({ whitelist: true }));
    await app.init();
  });
  afterAll(() => app.close());

  it("rejects invalid email", () =>
    request(app.getHttpServer())
      .post("/users")
      .send({ email: "nope", password: "secret123" })
      .expect(400));
});
```

Unit-test a provider by mocking its dependencies through the DI container:

```ts
const mod = await Test.createTestingModule({
  providers: [UsersService, { provide: getRepositoryToken(User), useValue: mockRepo }],
}).compile();
```

## Anti-patterns

- Business logic in controllers — controllers only route; logic belongs in `@Injectable` services.
- Skipping the global `ValidationPipe` / DTO decorators, then trusting raw request bodies.
- Instantiating providers with `new` instead of constructor injection — breaks DI and testability.
- Circular module dependencies from over-splitting; use `forwardRef` only as a last resort.
- Reading `process.env` directly instead of `ConfigService`.
- Returning entities with sensitive fields (password hashes) — use response DTOs or `class-transformer` `@Exclude`.
- Catching errors ad hoc in controllers instead of exception filters / built-in HTTP exceptions.
