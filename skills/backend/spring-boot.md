# spring-boot

> Source: curated best practices | 2026

---

# Spring Boot - Java REST services with Spring 6

This skill equips the coder to build production Spring Boot 3.2+ services on Java 21, using Spring Web MVC, Spring Data JPA, Bean Validation, and Spring Security. It enforces constructor injection, DTO records separate from JPA entities, `@Transactional` service layer, `@RestControllerAdvice` for centralized error handling, config via `application.yml` + `@ConfigurationProperties`, and `@SpringBootTest`/`@WebMvcTest` with MockMvc. Assumes Maven or Gradle, Lombok optional, and layered package-by-feature structure.

## When to Activate

Use when building with Spring Boot:
- Building Java REST APIs or microservices with Spring
- Files with `@RestController`, `@Service`, `@Entity`, `@SpringBootApplication`
- Adding endpoints, JPA repositories, validation, or Spring Security config
- Exception handling with `@RestControllerAdvice` or transactional services

## Patterns and Best Practices

Package by feature:

```
com.example.app/
  Application.java
  user/
    UserController.java UserService.java UserRepository.java
    User.java              # JPA entity
    UserDtos.java          # request/response records
  common/GlobalExceptionHandler.java
```

Entity with JPA annotations:

```java
// user/User.java
@Entity
@Table(name = "users", indexes = @Index(columnList = "email", unique = true))
public class User {
    @Id @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, unique = true)
    private String email;

    @Column(nullable = false)
    private String hashedPassword;

    @CreationTimestamp
    private Instant createdAt;
    // constructors, getters, setters
}
```

DTOs as records — never expose entities directly:

```java
// user/UserDtos.java
public record CreateUserRequest(
    @Email @NotBlank String email,
    @NotBlank @Size(min = 8) String password) {}

public record UserResponse(Long id, String email) {
    static UserResponse from(User u) { return new UserResponse(u.getId(), u.getEmail()); }
}
```

Repository via Spring Data:

```java
// user/UserRepository.java
public interface UserRepository extends JpaRepository<User, Long> {
    Optional<User> findByEmail(String email);
    boolean existsByEmail(String email);
}
```

Service with constructor injection and transactions:

```java
// user/UserService.java
@Service
public class UserService {
    private final UserRepository repo;
    private final PasswordEncoder encoder;

    public UserService(UserRepository repo, PasswordEncoder encoder) {
        this.repo = repo;
        this.encoder = encoder;
    }

    @Transactional
    public UserResponse create(CreateUserRequest req) {
        if (repo.existsByEmail(req.email()))
            throw new ConflictException("email already registered");
        var user = new User();
        user.setEmail(req.email());
        user.setHashedPassword(encoder.encode(req.password()));
        return UserResponse.from(repo.save(user));
    }
}
```

Controller — thin, validated:

```java
// user/UserController.java
@RestController
@RequestMapping("/users")
public class UserController {
    private final UserService service;
    public UserController(UserService service) { this.service = service; }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public UserResponse create(@Valid @RequestBody CreateUserRequest req) {
        return service.create(req);
    }

    @GetMapping("/{id}")
    public UserResponse get(@PathVariable Long id) {
        return service.get(id);
    }
}
```

Centralized error handling:

```java
// common/GlobalExceptionHandler.java
@RestControllerAdvice
public class GlobalExceptionHandler {
    record ApiError(String message) {}

    @ExceptionHandler(ConflictException.class)
    @ResponseStatus(HttpStatus.CONFLICT)
    ApiError conflict(ConflictException e) { return new ApiError(e.getMessage()); }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    @ResponseStatus(HttpStatus.BAD_REQUEST)
    ApiError invalid(MethodArgumentNotValidException e) {
        var msg = e.getBindingResult().getFieldErrors().stream()
            .map(f -> f.getField() + ": " + f.getDefaultMessage())
            .collect(Collectors.joining(", "));
        return new ApiError(msg);
    }
}
```

Security config (Spring Security 6 lambda DSL):

```java
@Configuration
@EnableWebSecurity
public class SecurityConfig {
    @Bean
    SecurityFilterChain chain(HttpSecurity http) throws Exception {
        return http
            .csrf(AbstractHttpConfigurer::disable)
            .authorizeHttpRequests(auth -> auth
                .requestMatchers(HttpMethod.POST, "/users").permitAll()
                .anyRequest().authenticated())
            .oauth2ResourceServer(o -> o.jwt(Customizer.withDefaults()))
            .build();
    }

    @Bean PasswordEncoder passwordEncoder() { return new BCryptPasswordEncoder(); }
}
```

Config binding instead of scattered `@Value`:

```java
@ConfigurationProperties(prefix = "app.jwt")
public record JwtProperties(String issuer, Duration expiry) {}
```

Slice test with MockMvc:

```java
// user/UserControllerTest.java
@WebMvcTest(UserController.class)
class UserControllerTest {
    @Autowired MockMvc mvc;
    @MockBean UserService service;

    @Test
    void rejectsShortPassword() throws Exception {
        mvc.perform(post("/users")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"email\":\"a@b.com\",\"password\":\"short\"}"))
           .andExpect(status().isBadRequest());
    }
}
```

## Anti-patterns

- Field injection with `@Autowired` — use constructor injection (final fields, testable, immutable).
- Returning JPA entities from controllers — leaks schema, triggers lazy-loading serialization errors.
- Business logic in controllers instead of a `@Service`; missing `@Transactional` on multi-write ops.
- Catching exceptions per-controller instead of `@RestControllerAdvice`.
- `spring.jpa.hibernate.ddl-auto=update` in production — use Flyway/Liquibase migrations.
- Lazy associations serialized in the web layer causing `LazyInitializationException` — map to DTOs.
- Scattering `@Value("${...}")` everywhere instead of typed `@ConfigurationProperties`.
