module Pages.Home exposing (view)

import Html exposing (Html, div, h1, h2, h3, p, button, text, img, section)
import Html.Attributes exposing (class, style, src, alt)
import Components.Banner as Banner
import Components.Widget as Widget

view : Html msg
view =
    div [ class "home-page" ]
        [ -- Hero Section
          section [ class "hero-section" ]
            [ div [ class "hero-content" ]
                [ div [ class "hero-text" ]
                    [ h1 [ class "hero-title" ] [ text "Modern Finance Solutions" ]
                    , p [ class "hero-subtitle" ] [ text "Discover powerful tools for managing your finances, from loan calculations to investment tracking. Built with cutting-edge technology for the modern world." ]
                    , div [ class "hero-buttons" ]
                        [ button [ class "btn-primary" ] [ text "Get Started" ]
                        , button [ class "btn-secondary" ] [ text "Learn More" ]
                        ]
                    ]
                , div [ class "hero-image" ]
                    [ div [ class "hero-placeholder" ] [ text "📱💳" ]
                    ]
                ]
            ]
        
        -- Features Section
        , section [ class "features-section" ]
            [ div [ class "container" ]
                [ h2 [ class "section-title" ] [ text "Why Choose Our Platform?" ]
                , div [ class "features-grid" ]
                    [ div [ class "feature-card" ]
                        [ div [ class "feature-icon" ] [ text "🧮" ]
                        , h3 [] [ text "Smart Calculator" ]
                        , p [] [ text "Advanced loan payment calculator with detailed amortization schedules and real-time calculations." ]
                        ]
                    , div [ class "feature-card" ]
                        [ div [ class "feature-icon" ] [ text "📊" ]
                        , h3 [] [ text "Visual Analytics" ]
                        , p [] [ text "Beautiful charts and graphs to help you understand your financial data at a glance." ]
                        ]
                    , div [ class "feature-card" ]
                        [ div [ class "feature-icon" ] [ text "🔒" ]
                        , h3 [] [ text "Secure & Private" ]
                        , p [] [ text "Your financial data stays private with our secure, client-side calculations and no data storage." ]
                        ]
                    , div [ class "feature-card" ]
                        [ div [ class "feature-icon" ] [ text "⚡" ]
                        , h3 [] [ text "Lightning Fast" ]
                        , p [] [ text "Built with Elm for instant calculations and smooth, responsive user experience." ]
                        ]
                    ]
                ]
            ]
        
        -- CTA Section
        , section [ class "cta-section" ]
            [ div [ class "container" ]
                [ h2 [] [ text "Ready to Take Control of Your Finances?" ]
                , p [] [ text "Start using our powerful tools today and make informed financial decisions." ]
                , button [ class "btn-primary btn-large" ] [ text "Start Calculating" ]
                ]
            ]
        ] 